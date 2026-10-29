"""The goal contract: timeouts and model gating, escalation markers, prompt composition, ``GoalRunner`` and ``SubprocessConfig``.

Split out of ``goal_runner.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
import signal
from typing import Any, Callable, List, Optional, Tuple
from dataclasses import dataclass

from .adapters import DeepResult, DeepRunner
from .runtime_settings import DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS as _DEFAULT_REVIEW_INTERVAL, core_settings


_log = logging.getLogger("quest-ai-runner.goal_runner")


DEFAULT_DEEP_MAX_TURNS = 30


# A deep run has NO fixed wall-clock kill by default. A long run is often a healthy one (a big
# refactor, a monitor loop), and a blunt timer killed runs moments before they committed their
# changes. Instead, every ``DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS`` a quick LLM reads the tail of
# the run's live session log and decides whether it should still be running (stalled, out of
# tokens, blocked on a decision). It stops the run only on a yes; otherwise the run continues and
# the one-sentence reason it was left alone goes to the log. Knobs:
#   QAR_DEEP_REVIEW_INTERVAL_SECONDS  seconds between reviews (default 300; 0 turns reviews off)
#   QAR_DEEP_TIMEOUT_SECONDS          OPTIONAL hard wall-clock cap; unset or 0 means no cap
DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS = _DEFAULT_REVIEW_INTERVAL


QAR_DEEP_REVIEW_INTERVAL_SECONDS_ENV_VAR = "QAR_DEEP_REVIEW_INTERVAL_SECONDS"


QAR_DEEP_TIMEOUT_SECONDS_ENV_VAR = "QAR_DEEP_TIMEOUT_SECONDS"


# The balanced tier on the Claude CLI. A deep run with no Claude model of its own runs on this, so no
# default ever reaches the CLI's own choice, which can be Opus.
BALANCED_CLI_MODEL = "sonnet"


DEFAULT_DEEP_REVIEW_MODEL = BALANCED_CLI_MODEL


DEEP_REVIEW_CALL_TIMEOUT_SECONDS = 120.0


# How often the progress monitor emits a liveness beat while otherwise quiet (before the session
# file exists, or while it exists but nothing new has been written since). Design rule: silence
# longer than about 10s while a deep run is working is a bug by definition.
DEFAULT_MONITOR_HEARTBEAT_SECONDS = 10.0


def _positive_float(raw: Optional[str], name: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _log.warning("%s=%r is not a valid number, ignoring it", name, raw)
        return None
    return value if value > 0 else None


def resolve_deep_timeout_seconds(configured: Optional[float]) -> Optional[float]:
    """The OPTIONAL hard wall-clock cap for the deep subprocess, or None for no cap.

    ``configured`` (``SubprocessConfig.timeout_seconds``) wins when the consumer set it
    explicitly, else ``QAR_DEEP_TIMEOUT_SECONDS``. Unset or non-positive means no fixed cap: the
    liveness review (``resolve_deep_review_interval_seconds``) is what decides a run should stop.
    """
    if configured is not None:
        return float(configured) if float(configured) > 0 else None
    return core_settings().deep_timeout_seconds


def resolve_deep_review_interval_seconds(configured: Optional[float]) -> Optional[float]:
    """Seconds between liveness reviews, or None when reviews are off (interval 0)."""
    if configured is not None:
        return float(configured) if float(configured) > 0 else None
    return core_settings().deep_review_interval_seconds


DEEP_REVIEW_PROMPT = """You are a watchdog for an AI worker running a long task. Below is the tail of its live session log. Decide whether the worker should STILL be running.

Answer stop=true ONLY if the log shows one of these:
- nothing has happened for more than 5 minutes (see the idle time below) with no legitimate reason
- it ran out of Claude/LLM tokens or hit a usage or rate limit
- it is blocked on the task or is asking for or waiting on a human decision, answer, approval, confirmation or credential (nobody is watching this run, so a question to a person will never be answered, however reasonable the question is)
- it is stuck repeating the SAME COMPLETE action with no new progress between repeats

Answer stop=false if it is making progress, or doing a legitimately slow thing for the task such as monitoring, waiting on a build, a test run, or a long command. Waiting on a machine or process is legitimate; waiting on a person is not. Commands that merely share a long common prefix (several ``cd``/``$`` lines into the same directory tree, or different commands under the same long path) are NOT a repeat; judge repetition only by whether the FULL line, including whatever comes after that shared prefix, is identical across entries. When unsure, answer stop=false: a wrong stop loses work that was about to finish.

Time the run has been going: {elapsed}s. Seconds since the log last changed: {idle}s.

Recent log (most recent last):
{tail}

Reply with ONLY one JSON object, no other text: {{"stop": true|false, "reason": "<one plain sentence>"}}"""


def parse_deep_review(text: str) -> Optional[tuple]:
    """(stop, reason) from the reviewer's reply, or None when it cannot be read."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001 — an unreadable verdict means "keep running"
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("stop"), bool):
        return None
    reason = " ".join(str(obj.get("reason") or "").split())
    return obj["stop"], (reason or "No reason was given.")


def kill_process_group(proc: "subprocess.Popen") -> None:
    """Kill the subprocess AND every child it spawned, not just the top pid.

    The subprocess is launched with ``start_new_session=True`` so it is its own process group
    leader; ``os.killpg`` reaches everything under it (Claude Code, or any tool it shells out to,
    can spawn children that would otherwise be orphaned by killing only the top pid). Falls back
    to ``proc.kill()`` alone if the process group cannot be resolved or signaled (already exited,
    or a platform without process groups).
    """
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError, AttributeError) as e:
        _log.warning("could not kill process group for pid %s (%s), falling back to proc.kill()", pid, e)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — best-effort cleanup, never raise from a timeout handler
        pass


# How often (seconds) a running worker is checked for a cancel request. Short, because a person who
# pressed Stop is waiting on it; the check itself is a cheap call the caller already throttles.
CANCEL_POLL_SECONDS = 1.0

# How long a worker is given to exit on its own after an interrupt before it is terminated, and
# after a terminate before it is killed. Claude Code flushes its session on an interrupt, so the
# grace is what keeps the work done so far resumable.
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


def signal_process_group(proc: "subprocess.Popen", sig: int) -> bool:
    """Send ``sig`` to the worker's whole process group; True when it was delivered.

    Same reach as ``kill_process_group`` (the worker is its own group leader), but with a chosen
    signal, so a stop can ask politely (SIGINT, then SIGTERM) before the SIGKILL.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        proc.send_signal(sig)
        return True
    except Exception:  # noqa: BLE001 -- already exited, or no such process
        return False


def stop_process_group(proc: "subprocess.Popen", grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS
                       ) -> Tuple[Optional[bytes], Optional[bytes], str]:
    """Stop a running worker promptly: interrupt, then terminate, then kill.

    SIGINT first (what Esc is to Claude Code: it stops the current step and exits having saved its
    session), SIGTERM after ``grace_seconds`` if it is still alive, SIGKILL after another
    ``grace_seconds``. Returns ``(stdout, stderr, how)`` with whatever the worker had buffered and
    ``how`` one of ``"interrupted"``, ``"terminated"`` or ``"killed"``. Never raises.
    """
    import subprocess as _subprocess
    for sig, how in ((signal.SIGINT, "interrupted"), (signal.SIGTERM, "terminated")):
        signal_process_group(proc, sig)
        try:
            out, err = proc.communicate(timeout=grace_seconds)
            return out, err, how
        except _subprocess.TimeoutExpired:
            continue
        except Exception:  # noqa: BLE001 -- a broken pipe means it is gone
            return None, None, how
    kill_process_group(proc)
    try:
        out, err = proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001 -- best-effort drain after a kill
        out, err = None, None
    return out, err, "killed"


# Which model ids the Claude Code worker can run: the ids and bedrock ids that name Claude or Anthropic, and the bare tier
# aliases Claude Code accepts. A pattern constant (data), not a branch on a vendor name.
CLAUDE_CODE_MODEL_PATTERN = re.compile(r"claude|anthropic|^(opus|sonnet|haiku|fable)")


def _is_claude_model(model: str) -> bool:
    """Whether ``model`` is a Claude model the Claude Code worker can actually run.

    Claude Code only runs Claude models; a tier resolved from a Gemini/OpenAI deployment (e.g.
    ``gemini-3.5-flash``, ``gpt-4o``) must NOT be passed as ``--model`` or the worker errors and
    does nothing. Accepts Claude ids/aliases (``claude-...``, bare ``opus``/``sonnet``/``haiku``/
    ``fable``, ``us.anthropic.claude-...`` bedrock ids); rejects everything else."""
    return bool(CLAUDE_CODE_MODEL_PATTERN.search((model or "").strip().lower()))


# The model translator: ``str -> Optional[str]`` mapping a model id to the value ``claude --model``
# can invoke (or None for a model the worker cannot run). It is INJECTED, never imported: ``core``
# must not depend on ``adapters``, so the composition root registers the adapter's translator
# (``claude_cli_provider.cli_model``) with ``set_cli_model_translator``, and a runner can be handed
# its own through ``SubprocessGoalRunner(model_translator=...)``. Unset, only the syntactic
# ``_is_claude_model`` gate applies.
_cli_model_translator: Optional[Callable[[Optional[str]], Optional[str]]] = None


def set_cli_model_translator(
        translator: Optional[Callable[[Optional[str]], Optional[str]]]) -> None:
    """Register the process-wide model translator ``cli_safe_model`` falls back to (None clears it)."""
    global _cli_model_translator
    _cli_model_translator = translator


def cli_safe_model(model: Optional[str],
                   translator: Optional[Callable[[Optional[str]], Optional[str]]] = None
                   ) -> Optional[str]:
    """The value to actually pass as ``claude --model``, or None to pass nothing.

    ``_is_claude_model`` answers a purely SYNTACTIC question ("is this string Claude-shaped?"),
    which is not the same as "is this a model id the CLI can invoke". A tier can resolve to a
    FAMILY-BUCKET LABEL — ``claude-sonnet``, from ``ClaudeCliProvider.CLI_RUNNABLE_MODELS``, which
    exists so ``ModelRegistry.bucket_top`` can bucket tiers on a CLI-only deployment with no live
    model list. That label is Claude-shaped but is NOT invokable: passing it through made the real
    binary answer "There's an issue with the selected model (claude-sonnet). It may not exist or
    you may not have access to it", identically on every retry, so the whole deep run was inert.

    The translator for exactly this lives next to the labels it invents
    (``claude_cli_provider.cli_model``: family → the bare alias the CLI accepts, non-Claude → None,
    other Claude ids unchanged); the shallow plan/answer path has used it since it was added, and
    this deep path was simply missed. ``core`` must not depend on ``adapters``, so the translator
    is injected: ``translator`` if given, else the one registered with ``set_cli_model_translator``,
    and with neither this degrades to the old syntactic gate.
    """
    translate = translator or _cli_model_translator
    if translate is None:   # no adapter wired: fall back to the syntactic gate
        return model if _is_claude_model(model or "") else None
    try:
        return translate(model)
    except Exception:  # noqa: BLE001 — a translator failure must never break the spawn
        return model if _is_claude_model(model or "") else None


def _run_goal_accepts_context_preamble(runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``context_preamble`` keyword (or **kwargs).

    Mirrors the orchestrator's ``emit`` capability check: a per-call preamble is forwarded ONLY to
    runners that opt in by accepting the kwarg, leaving older ``run_goal`` signatures untouched.
    Decided by signature inspection rather than try/except so a runner with a side effect is never
    invoked twice.
    """
    try:
        sig = inspect.signature(runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "context_preamble" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_working_dir(runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``working_dir`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_context_preamble``: a per-call working-directory
    override (e.g. a quest's synced folder, see ``quest_autopilot_design.md``'s execution-
    environment section) is forwarded ONLY to a runner that accepts it, so older ``run_goal``
    signatures are untouched.
    """
    try:
        sig = inspect.signature(runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "working_dir" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _run_goal_accepts_resume_session_id(runner: Any) -> bool:
    """Whether a DeepRunner's ``run_goal`` accepts a ``resume_session_id`` keyword (or **kwargs).

    Same opt-in discipline as ``_run_goal_accepts_context_preamble``. A caller continuing an
    attempt that ran out of turns forwards the worker's session id ONLY to a runner that can
    resume it; everyone else runs the goal fresh, exactly as before.
    """
    try:
        sig = inspect.signature(runner.run_goal)
    except (ValueError, TypeError, AttributeError):
        return False
    for p in sig.parameters.values():
        if p.name == "resume_session_id" or p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


# The escalation-marker contract: a spawned worker that raised a human decision mid-run (via
# whatever escalation mechanism its consumer preamble gave it) reports the decision back to the
# runner by printing, on its own line, ``QAR-ESCALATED: <decision_id>``. SubprocessGoalRunner
# parses the marker and returns ``DeepResult(met=False, decision_id=...)``, which the executor
# reports as ``needs_you`` with the decision linked — so the ask surfaces in the consumer's UI
# attached to the paused task instead of the task closing as done/failed. Workers that never
# escalate are unaffected; the marker simply never appears.
ESCALATION_MARKER = "QAR-ESCALATED:"


# Matches the marker ANYWHERE in a line (not just at its start), capturing the first
# whitespace-delimited token that follows it as the candidate id.
ESCALATION_MARKER_ANYWHERE_RE = re.compile(re.escape(ESCALATION_MARKER) + r"\s*(\S+)")


# What a captured token has to look like to be accepted as a real decision id: 4-64 characters of
# id-shaped text only. Matching the marker mid-line (rather than only at a line's start) also
# matches it inside PROSE -- above all the instruction that teaches the convention in the first
# place ("print QAR-ESCALATED: <decision_id> when you escalate"), which a worker can echo back in
# its own summary. Accepting that would pause a finished task on a decision id that does not
# exist, which is a worse failure than the orphan this widening was meant to fix. Placeholders
# (``<decision_id>``, ``{id}``, ``[id]``), sentence words and punctuation are all excluded by the
# charset and the minimum length; the orchestrator's own open-decision diff still recovers a
# genuine escalation whose marker never landed.
ESCALATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{3,63}$")


def extract_escalation_id(output: str) -> Optional[str]:
    """Return the decision id from the LAST ``QAR-ESCALATED: <id>`` marker, or None.

    The marker is matched anywhere WITHIN a line, not only when it starts one: a worker's final
    message can lead into it or wrap it in other text ("Note: QAR-ESCALATED: dec_123" or a bullet
    list item), and requiring the line to start with the marker silently dropped those runs --
    they closed as done with an orphaned open decision instead of pausing on it. Trailing
    punctuation the model might tack on (a period, a closing paren/quote) is stripped from the id.
    A candidate that is not ID-SHAPED (``ESCALATION_ID_RE``) is ignored, so the convention being
    described in prose, or a ``<decision_id>`` placeholder echoed back, is never mistaken for a
    real escalation.
    """
    decision_id: Optional[str] = None
    for line in (output or "").splitlines():
        for match in ESCALATION_MARKER_ANYWHERE_RE.finditer(line):
            candidate = match.group(1).strip().rstrip(").,;:\"'")
            if candidate and ESCALATION_ID_RE.match(candidate):
                decision_id = candidate
    return decision_id


# What Claude Code says, on stderr, when asked to resume a session it no longer holds. Measured,
# not assumed (2026-09-18): `claude -p --output-format json --resume <unknown-uuid>` exits 1, prints
# NOTHING on stdout, and writes exactly "No conversation found with session ID: <id>" on stderr.
# Sessions are per project directory and are pruned over time, so an across-run resume of a session
# from days ago meets this routinely; it is an ordinary outcome, not an error the person should see.
RESUME_TARGET_MISSING_MARKERS = ("no conversation found", "no session found")


def resume_target_missing(returncode: int, output: str, stderr: Optional[str]) -> bool:
    """Whether this failure is "the session to resume is gone", and nothing else.

    Deliberately narrow, because the response to it (throw the run away and start it again cold) is
    the WRONG response to any other failure. All three must hold: the worker exited non-zero, it
    produced no result text at all (it never got as far as running the goal), and its own stderr
    says the conversation was not found. A run that resumed fine and then failed has output, a
    different error, or both, so it is never mistaken for this.
    """
    if returncode == 0 or (output or "").strip():
        return False
    low = (stderr or "").strip().lower()
    return any(marker in low for marker in RESUME_TARGET_MISSING_MARKERS)


def _parse_worker_output(raw: str) -> tuple:
    """Parse Claude Code's ``--output-format json`` envelope into
    (result_text, tokens, cost, is_error, subtype).

    Returns the final result text, the total tokens (input+output) the worker reported, the cost in
    USD, whether the worker flagged an error, and the envelope's ``subtype`` — the worker's own
    statement of HOW it ended (``success``, ``error_max_turns``, ...), which is the difference
    between diagnosing a failure and guessing at it. Falls back to (raw, 0, 0.0, False, "") when
    the output is not the expected JSON (e.g. a plain-text worker), so a non-Claude-Code DeepRunner
    still works. Never raises."""
    text = raw or ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and ("result" in data or "usage" in data):
            text = str(data.get("result") or "")
            usage = data.get("usage") or {}
            tokens = 0
            if isinstance(usage, dict):
                tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            cost = float(data.get("total_cost_usd") or 0.0)
            return text, tokens, cost, bool(data.get("is_error")), str(data.get("subtype") or "")
    except Exception:  # noqa: BLE001 — any parse issue falls back to raw text, no usage
        pass
    return text, 0, 0.0, False, ""


def envelope_reports_no_work(raw: str) -> bool:
    """True when the worker's JSON envelope says it FAILED having used no model tokens at all.

    That is the shape of a worker that never started its goal: Claude Code rejected the run before
    its first model call (an unrecognized model id, bad credentials, a refused flag) and reports
    ``is_error`` with zero input and output tokens. Only a parsed envelope that carries ``usage``
    counts: plain-text output, or a worker killed before it printed its envelope, says nothing
    about how much work happened, so it is never classified as a launch failure here. Never raises.
    """
    try:
        data = json.loads(raw or "")
    except Exception:  # noqa: BLE001: not an envelope, so not a statement about work done
        return False
    if not isinstance(data, dict) or not data.get("is_error"):
        return False
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return False
    # Cache traffic counts as work too: a turn served largely from the prompt cache can report
    # very few plain input tokens.
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    try:
        used = sum(int(usage.get(k) or 0) for k in keys)
    except (TypeError, ValueError):
        return False
    return used == 0


def compose_goal_prompt(goal: str, brief: str, *, preamble: str = "",
                        budget_tokens: Optional[int] = None,
                        model: Optional[str] = None) -> str:
    """The headless worker prompt, fitted to the deep prompt budget (see ``compose_goal_prompt_fitted``)."""
    return compose_goal_prompt_fitted(goal, brief, preamble=preamble, budget_tokens=budget_tokens,
                                      model=model)[0]


def compose_goal_prompt_fitted(goal: str, brief: str, *, preamble: str = "",
                               budget_tokens: Optional[int] = None,
                               model: Optional[str] = None) -> Tuple[str, Any]:
    """Compose the headless worker prompt: an optional preamble + the TASK brief + the GOAL stated
    as a plain-text done-standard. Returns ``(prompt, fit)``, ``fit`` being the
    ``prompt_budget.FitResult`` that says what, if anything, was cut.

    THE PROMPT IS BUDGETED HERE, the one place every piece meets (``core.prompt_budget``). The
    preamble's blocks (doctrine, persona, retrieved conversations, cards) are cut by priority when
    the whole would exceed ``budget_tokens`` (None resolves the configured budget, capped at what
    ``model``'s window can hold), and exactly one context-updates block survives. Under budget the
    prompt is byte-identical to the unbudgeted composition.

    We deliberately do NOT use Claude Code's ``/goal`` directive. ``/goal`` runs its OWN internal
    verify-the-condition-every-turn loop (token-heavy) and caps the condition at 4000 chars
    (a longer one is rejected and the worker runs nothing). Instead the ORCHESTRATOR runs its own
    goal loop: it verifies the done-standard with one cheap LLM call after the worker returns, and
    re-runs with targeted guidance if it is not yet met. So here the worker just attempts the task
    once and reports concretely what it changed; the done-standard is given as context, not as a
    self-policed directive."""
    from . import prompt_budget
    from .standing_instructions import load_standing_instructions
    goal_text = " ".join((goal or "").split())
    body_parts: List[str] = []
    # Standing instructions are a REQUIRED body part, not a preamble block: a rep's persona
    # replaces the preamble and the budget cuts preamble blocks, but these govern every run.
    standing = load_standing_instructions()
    if standing:
        body_parts.append(standing)
    body_parts.append(f"TASK:\n{brief.strip()}")
    if goal_text:
        body_parts.append("GOAL (the done-standard your work must satisfy):\n" + goal_text)
    body_parts.append(
        "CONTEXT USAGE: The brain has already identified the relevant files and sources above — "
        "treat that as your starting point and work from it directly. "
        "Do NOT run an Explore phase or broad codebase-wide discovery sweep. "
        "Targeted lookups are fine: grep for a specific symbol, read a specific file, "
        "or run a quick find to locate something the context didn't cover. "
        "Avoid any broad exploration that duplicates what the context already provides."
    )
    body_parts.append(
        "HEADLESS RUN CONTRACT: You are running non-interactively; the process exits the moment "
        "you produce your final message, and any subagents still running in the background are "
        "killed at that instant, their work lost. Never end your turn while delegated work is "
        "pending: if you launch subagents, wait for every one to finish and fold its results into "
        "work you have verified yourself before writing the final summary. A final message that "
        "promises ongoing or future work (\"agents are still running\", \"I'll review and commit "
        "next\") means that work will simply never happen; it is a failed run, not a handoff."
    )
    body_parts.append(
        "When done, summarize CONCRETELY what you changed (the files and the actual edits/actions). "
        "If you could not fully meet the goal, say exactly what remains and why."
    )
    budget = prompt_budget.resolve_deep_prompt_budget(budget_tokens, model=model)
    fitted_preamble, fitted_parts, fit = prompt_budget.fit_deep_prompt(
        (preamble or "").strip(), body_parts, budget_tokens=budget)
    head = [fitted_preamble] if fitted_preamble.strip() else []
    return "\n\n".join(head + [p for p in fitted_parts if p]), fit


class GoalRunner:
    """Adds the goal-contract bookkeeping around any DeepRunner.

    The brain hands ``GoalRunner`` a goal + brief; it delegates execution to the wrapped
    DeepRunner and returns a normalized DeepResult (met vs limit/error). The point of the
    wrapper is that the met-vs-limit semantics live HERE, generically, regardless of worker.
    """

    def __init__(self, runner: DeepRunner, *, default_max_turns: int = DEFAULT_DEEP_MAX_TURNS):
        self._runner = runner
        self._default_max_turns = default_max_turns

    def run(self, *, goal: str, brief: str, model: Optional[str] = None,
            max_turns: Optional[int] = None,
            context_preamble: Optional[str] = None,
            working_dir: Optional[str] = None,
            resume_session_id: Optional[str] = None) -> DeepResult:
        turns = max_turns if max_turns is not None else self._default_max_turns
        try:
            kwargs = dict(goal=goal, brief=brief, model=model, max_turns=turns)
            # Forward a per-call preamble ONLY to a wrapped runner that accepts it, so older
            # DeepRunner signatures (no ``context_preamble`` kwarg) keep working unchanged.
            if context_preamble is not None and _run_goal_accepts_context_preamble(self._runner):
                kwargs["context_preamble"] = context_preamble
            # Same opt-in discipline for a per-call working-directory override (e.g. a quest's
            # synced folder for THIS run only, falling back to the runner's configured default).
            if working_dir is not None and _run_goal_accepts_working_dir(self._runner):
                kwargs["working_dir"] = working_dir
            # Continuing a turn-budget-exhausted attempt in the worker's own session, same rule.
            if (resume_session_id is not None
                    and _run_goal_accepts_resume_session_id(self._runner)):
                kwargs["resume_session_id"] = resume_session_id
            res = self._runner.run_goal(**kwargs)
        except Exception as e:  # noqa: BLE001 — the goal contract never raises to the caller
            return DeepResult(met=False, error=f"deep runner failed: {type(e).__name__}")
        # Normalize: a runner that forgot to set met but returned an error is "not met".
        if res.error and res.met:
            res.met = False
        # A run that raised a human decision is paused, never "met" — the executor reports
        # needs_you from decision_id, and "met" would short-circuit that to done.
        if res.decision_id and res.met:
            res.met = False
        return res


# The Claude Code web tools. Their PRESENCE in the spawned worker's tool set is exactly what makes
# the deep-runner able to BROWSE/SEARCH the live internet — so the env can honestly advertise web.
WEB_TOOLS = ("WebSearch", "WebFetch")


@dataclass
class SubprocessConfig:
    """Config for the reference subprocess runner — all consumer-supplied, NO defaults baked in.

    The spawned worker is Claude Code, which ships ``WebSearch``/``WebFetch`` — so by default
    (``skip_permissions=True``, no tool restrictions) the deep-runner CAN browse the live web. The
    ``allowed_tools``/``disallowed_tools`` fields make that explicit and constrainable: if a
    consumer pins ``allowed_tools`` to a set without the web tools (or disallows them), web is OFF
    and the env reports it honestly. ``web_enabled()`` is the single source of truth for that
    derivation, read by the runner's capability heartbeat.
    """
    working_dir: str                          # where the worker runs (consumer's corpus root)
    claude_path: str = "claude"               # the worker binary (looked up on PATH if bare)
    context_preamble: str = ""                # optional org/persona context prepended to the brief
    skip_permissions: bool = True             # --dangerously-skip-permissions for headless runs
    extra_path_dirs: Optional[List[str]] = None   # dirs to prepend to PATH for the subprocess
    # OPTIONAL hard wall-clock cap on the subprocess. None reads QAR_DEEP_TIMEOUT_SECONDS; if that
    # is unset too there is no fixed cap and the liveness review decides when a run must stop.
    timeout_seconds: Optional[float] = None
    # Liveness review (see DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS). ``review_interval_seconds`` None
    # reads QAR_DEEP_REVIEW_INTERVAL_SECONDS (default 300), 0 turns reviews off. ``reviewer`` is
    # an optional ``prompt -> reply text`` callable (tests, or a consumer's own cheap model);
    # unset, a one-shot ``claude -p --model <review_model>`` call is used.
    review_interval_seconds: Optional[float] = None
    review_model: str = DEFAULT_DEEP_REVIEW_MODEL
    reviewer: Optional[Callable[[str], str]] = None
    # Tool gating for the spawned Claude Code worker (generic, optional):
    #   * allowed_tools=None  → don't pass --allowed-tools; the worker has its DEFAULT tool set
    #     (which includes WebSearch/WebFetch). With skip_permissions this is the full, web-capable set.
    #   * allowed_tools=[...] → pass --allowed-tools; web is available only if a web tool is listed.
    #   * disallowed_tools=[...] → pass --disallowed-tools; listing a web tool turns web OFF.
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    # Path to an MCP server config file/JSON string passed straight through as ``claude
    # --mcp-config <path>`` (confirmed present in the installed ``claude`` CLI's --help; loads MCP
    # servers from JSON files or strings, space-separated -- this field passes exactly one). None
    # (the default) omits the flag entirely, i.e. today's behavior: whatever MCP servers the
    # worker's own ambient config already provides. This does NOT imply ``--strict-mcp-config``
    # (which restricts the worker to ONLY these servers, ignoring its ambient config) -- that is a
    # separate, stricter policy left for a consumer to opt into explicitly if a later phase wires it.
    mcp_config_path: Optional[str] = None
    # The token budget for the composed deep prompt (``core.prompt_budget``). None reads
    # QAR_DEEP_PROMPT_TOKEN_BUDGET, else the library default; any value is capped at what the
    # run's model window can hold beside the worker's own system prompt and working room.
    prompt_token_budget: Optional[int] = None
    # Seconds a worker is given to exit after an interrupt (and again after a terminate) when a run
    # is stopped on request, before it is killed. See ``stop_process_group``.
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS

    def web_enabled(self) -> bool:
        """Whether the spawned worker can BROWSE the live web (WebSearch/WebFetch reachable).

        Honest derivation from the actual tool gating:
          * If any web tool is explicitly disallowed → False.
          * If allowed_tools is pinned → True only when it includes a web tool.
          * Otherwise (default tool set) → True: Claude Code ships the web tools, and with
            skip_permissions they're usable without a per-call prompt. (Even without
            skip_permissions, the tools EXIST; a headless run just couldn't get interactive
            approval — but the default headless contract here is skip_permissions=True.)
        """
        disallowed = {t.strip() for t in (self.disallowed_tools or [])}
        if any(t in disallowed for t in WEB_TOOLS):
            return False
        if self.allowed_tools is not None:
            allowed = {t.strip() for t in self.allowed_tools}
            return any(t in allowed for t in WEB_TOOLS)
        return True
