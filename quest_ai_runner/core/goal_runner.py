"""Goal runner — the "run to a written /goal done-standard, bounded by --max-turns" contract.

A generic goal-runner behind the ``DeepRunner`` interface. The contract (independent of WHICH
worker executes it):

  * The consumer (or the brain) authors a CONCRETE, CHECKABLE ``goal`` — a done-standard a human
    could verify ("all tests in test_x.py pass", "the one-pager covers problem/solution/pricing").
  * The run is BOUNDED by ``max_turns`` so it can't run away.
  * On exit we distinguish GOAL-MET (clean, exit 0) from LIMIT/ERROR (non-zero) — surfaced as
    ``DeepResult.met`` so the caller can report "done" vs "needs more".

This module provides:

  * ``GoalRunner`` — wraps any DeepRunner and adds the goal-contract bookkeeping (compose the
    prompt with the /goal directive + brief, enforce max_turns, interpret met-vs-limit). The
    brain/executor calls ``GoalRunner.run`` and gets a normalized DeepResult.
  * ``SubprocessGoalRunner`` — a reference DeepRunner that spawns Claude Code headless with
    ``/goal <goal>`` + ``--max-turns``, generalized: NO paths
    baked in — the working dir, the claude binary, model, and a context preamble are all config.
    A consumer that uses a different worker just implements DeepRunner instead.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, List, Optional

from .adapters import DeepResult, DeepRunner

_log = logging.getLogger("quest-ai-runner.goal_runner")

DEFAULT_DEEP_MAX_TURNS = 30


def _is_claude_model(model: str) -> bool:
    """Whether ``model`` is a Claude model the Claude Code worker can actually run.

    Claude Code only runs Claude models; a tier resolved from a Gemini/OpenAI deployment (e.g.
    ``gemini-3.5-flash``, ``gpt-4o``) must NOT be passed as ``--model`` or the worker errors and
    does nothing. Accepts Claude ids/aliases (``claude-...``, bare ``opus``/``sonnet``/``haiku``,
    ``us.anthropic.claude-...`` bedrock ids); rejects everything else."""
    m = (model or "").strip().lower()
    if not m:
        return False
    if "claude" in m or "anthropic" in m:
        return True
    # Bare Claude tier aliases Claude Code accepts.
    return any(m == alias or m.startswith(alias) for alias in ("opus", "sonnet", "haiku"))


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

# The escalation-marker contract: a spawned worker that raised a human decision mid-run (via
# whatever escalation mechanism its consumer preamble gave it) reports the decision back to the
# runner by printing, on its own line, ``QAR-ESCALATED: <decision_id>``. SubprocessGoalRunner
# parses the marker and returns ``DeepResult(met=False, decision_id=...)``, which the executor
# reports as ``needs_you`` with the decision linked — so the ask surfaces in the consumer's UI
# attached to the paused task instead of the task closing as done/failed. Workers that never
# escalate are unaffected; the marker simply never appears.
ESCALATION_MARKER = "QAR-ESCALATED:"


def extract_escalation_id(output: str) -> Optional[str]:
    """Return the decision id from the LAST ``QAR-ESCALATED: <id>`` marker line, or None."""
    decision_id: Optional[str] = None
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(ESCALATION_MARKER):
            candidate = stripped[len(ESCALATION_MARKER):].strip()
            if candidate:
                decision_id = candidate
    return decision_id


def compose_goal_prompt(goal: str, brief: str, *, preamble: str = "") -> str:
    """Compose the headless worker prompt: an optional preamble + the TASK brief + the GOAL stated
    as a plain-text done-standard.

    We deliberately do NOT use Claude Code's ``/goal`` directive. ``/goal`` runs its OWN internal
    verify-the-condition-every-turn loop (token-heavy) and caps the condition at 4000 chars
    (a longer one is rejected and the worker runs nothing). Instead the ORCHESTRATOR runs its own
    goal loop: it verifies the done-standard with one cheap LLM call after the worker returns, and
    re-runs with targeted guidance if it is not yet met. So here the worker just attempts the task
    once and reports concretely what it changed; the done-standard is given as context, not as a
    self-policed directive."""
    goal_text = " ".join((goal or "").split())
    body_parts: List[str] = []
    if preamble.strip():
        body_parts.append(preamble.strip())
    body_parts.append(f"TASK:\n{brief.strip()}")
    if goal_text:
        body_parts.append("GOAL (the done-standard your work must satisfy):\n" + goal_text)
    body_parts.append(
        "EFFICIENCY: The context above shows the relevant files/sources already identified by the "
        "brain. Use them as your starting point. Only explore further if the provided context is "
        "insufficient to complete the goal. Avoid redundant exploration of files already listed above."
    )
    body_parts.append(
        "When done, summarize CONCRETELY what you changed (the files and the actual edits/actions). "
        "If you could not fully meet the goal, say exactly what remains and why."
    )
    return "\n\n".join(body_parts)


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
            context_preamble: Optional[str] = None) -> DeepResult:
        turns = max_turns if max_turns is not None else self._default_max_turns
        try:
            kwargs = dict(goal=goal, brief=brief, model=model, max_turns=turns)
            # Forward a per-call preamble ONLY to a wrapped runner that accepts it, so older
            # DeepRunner signatures (no ``context_preamble`` kwarg) keep working unchanged.
            if context_preamble is not None and _run_goal_accepts_context_preamble(self._runner):
                kwargs["context_preamble"] = context_preamble
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
    timeout_seconds: Optional[float] = None   # hard wall-clock cap on the subprocess
    # Tool gating for the spawned Claude Code worker (generic, optional):
    #   * allowed_tools=None  → don't pass --allowed-tools; the worker has its DEFAULT tool set
    #     (which includes WebSearch/WebFetch). With skip_permissions this is the full, web-capable set.
    #   * allowed_tools=[...] → pass --allowed-tools; web is available only if a web tool is listed.
    #   * disallowed_tools=[...] → pass --disallowed-tools; listing a web tool turns web OFF.
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None

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


class SubprocessGoalRunner(DeepRunner):
    """Reference DeepRunner: spawn Claude Code headless with ``/goal`` + ``--max-turns``.

    The working dir, binary, model, and any context preamble are CONFIG, not hardcoded
    paths. Exit code 0 = goal met cleanly; non-zero =
    hit the turn/budget limit or errored (DeepResult.met=False with a clear message).
    """

    def __init__(self, config: SubprocessConfig):
        self.cfg = config

    def _build_env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Don't let a spawned headless run reuse our own session / API billing.
        env.pop("CLAUDECODE", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if self.cfg.extra_path_dirs:
            cur = env.get("PATH", "")
            for d in self.cfg.extra_path_dirs:
                if d and d not in cur:
                    cur = f"{d}:{cur}"
            env["PATH"] = cur
        return env

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        # ``context_preamble`` is an OPTIONAL PER-CALL override of ``self.cfg.context_preamble``.
        # When the orchestrator forwards a per-task preamble (e.g. an AI rep's pulled persona), it
        # is used for THIS run only; otherwise the runner's configured base preamble applies, so
        # callers that pass nothing see exactly the prior behaviour.
        preamble = self.cfg.context_preamble if context_preamble is None else context_preamble
        prompt = compose_goal_prompt(goal, brief, preamble=preamble)
        binary = self.cfg.claude_path
        if os.path.sep not in binary:
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved
        # -p / --print: run Claude Code HEADLESS (non-interactive) and print the final result.
        # This is REQUIRED: Claude Code "starts an interactive session by default" and only runs
        # non-interactively under -p/--print. Without it, the binary launches the interactive TUI,
        # immediately hits EOF on the piped stdin prompt, and exits 0 having done NOTHING — which
        # this runner would record as met=True (a silent no-op that looks "Completed"). The -p flag
        # is what makes the deep run actually execute the goal and produce output.
        cmd: List[str] = [binary, "-p"]
        if self.cfg.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        # Apply explicit tool gating when the consumer pinned it (kept in lock-step with
        # web_enabled() so what the env ADVERTISES matches what the worker is actually allowed).
        if self.cfg.allowed_tools:
            cmd += ["--allowed-tools", ",".join(self.cfg.allowed_tools)]
        if self.cfg.disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(self.cfg.disallowed_tools)]
        # The worker is Claude Code, which ONLY runs Claude models. The orchestrator resolves the
        # model from the consumer's tier config, which in a Gemini/OpenAI deployment is a NON-Claude
        # id (e.g. "gemini-3.5-flash"). Passing that as --model makes Claude Code error ("issue with
        # the selected model ... it may not exist or you may not have access") and the deep run does
        # nothing. So pass --model ONLY when it is a Claude model; otherwise omit it and let the
        # worker use its own configured default.
        if model and _is_claude_model(model):
            cmd += ["--model", model]
        elif model:
            _log.debug("deep worker is Claude Code; ignoring non-Claude model %r, using its default",
                       model)
        turns = max_turns if max_turns is not None else DEFAULT_DEEP_MAX_TURNS
        if goal.strip():
            cmd += ["--max-turns", str(int(turns))]

        try:
            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),
                cwd=self.cfg.working_dir,
                env=self._build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.cfg.timeout_seconds,
            )
        except FileNotFoundError:
            return DeepResult(met=False, error=f"worker binary not found: {self.cfg.claude_path}")
        except PermissionError as e:
            return DeepResult(met=False, error=f"permission denied running worker: {e}")
        except subprocess.TimeoutExpired:
            return DeepResult(met=False, error="goal run exceeded the time limit before completing")

        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace") or None
        # The escalation-marker contract: the worker raised a human decision mid-run and printed
        # ``QAR-ESCALATED: <decision_id>``. That overrides met-vs-limit — the run is PAUSED on a
        # human, so the executor must report needs_you with the decision linked, regardless of
        # the exit code.
        decision_id = extract_escalation_id(out)
        if decision_id:
            return DeepResult(met=False, output=out, decision_id=decision_id)
        if proc.returncode == 0:
            # Safety net against a SILENT NO-OP: a real headless ``-p`` run always prints its final
            # result, so exit-0 with EMPTY output means the worker never actually ran the goal (the
            # failure mode when ``-p`` is missing, or the binary mis-launches). Report it as a
            # failure with a clear message instead of a hollow "met" that shows "Completed" but did
            # nothing. (A pure chit-chat run has an empty ``goal`` and is exempt.)
            if goal.strip() and not out.strip():
                return DeepResult(
                    met=False, output=out,
                    error="worker exited cleanly but produced NO output — the goal did not actually "
                          "run (check that the worker runs headless, e.g. Claude Code needs -p).")
            return DeepResult(met=True, output=out)
        if not err:
            err = (f"Goal run did not complete cleanly (exit {proc.returncode}) — likely hit the "
                   "turn/budget limit before fully meeting the goal.")
        return DeepResult(met=False, output=out, error=err)
