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

import hashlib
import inspect
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

from .adapters import DeepResult, DeepRunner, EVENT_EXEC, ProgressEvent

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


def _parse_worker_output(raw: str) -> tuple:
    """Parse Claude Code's ``--output-format json`` envelope into (result_text, tokens, cost, is_error).

    Returns the final result text, the total tokens (input+output) the worker reported, the cost in
    USD, and whether the worker flagged an error. Falls back to (raw, 0, 0.0, False) when the output
    is not the expected JSON (e.g. a plain-text worker), so a non-Claude-Code DeepRunner still works.
    Never raises."""
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
            return text, tokens, cost, bool(data.get("is_error"))
    except Exception:  # noqa: BLE001 — any parse issue falls back to raw text, no usage
        pass
    return text, 0, 0.0, False


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


def _find_claude_project_dir(working_dir: Optional[str] = None) -> Optional[Path]:
    """Find ANY .claude/projects directory with JSONL files.

    Claude stores sessions in {base}/.claude/projects/{project-key}/*.jsonl
    We don't know the exact project-key, so search for any dir with JSONL files.
    """
    candidates = []

    # Check working_dir's .claude/projects
    if working_dir:
        local_projects = Path(working_dir) / ".claude" / "projects"
        if local_projects.exists():
            candidates.append(local_projects)
            _log.info("_find_claude_project_dir: found .claude/projects in working_dir")

    # Check home .claude/projects
    home_projects = Path.home() / ".claude" / "projects"
    if home_projects.exists():
        candidates.append(home_projects)
        _log.info("_find_claude_project_dir: found .claude/projects in home")

    # Search each candidate for subdirs with JSONL files
    for base in candidates:
        try:
            for subdir in base.iterdir():
                if subdir.is_dir() and list(subdir.glob("*.jsonl")):
                    _log.info("_find_claude_project_dir: ✓ found project dir with JSONL: %s", subdir)
                    return subdir
        except Exception as e:
            _log.debug("_find_claude_project_dir: error searching %s: %s", base, e)

    _log.warning("_find_claude_project_dir: ✗ no .claude/projects dir with JSONL files found")
    return None


def _find_active_jsonl_file(project_dir: Path) -> Optional[Path]:
    """Find the most recently modified JSONL file with conversation content."""
    if not project_dir.exists():
        return None

    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None

    # Sort by modification time (most recent first)
    jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    # Find the first file with actual conversation content
    for jsonl_file in jsonl_files:
        try:
            with open(jsonl_file, 'r') as f:
                content = f.read()
                if '"type":"user"' in content or '"type":"assistant"' in content:
                    return jsonl_file
        except Exception:
            continue

    return jsonl_files[0] if jsonl_files else None


def _hash_line(line: str) -> str:
    """Create a hash of a line for deduplication."""
    return hashlib.md5(line.encode()).hexdigest()


def _format_message_text(msg: dict) -> str:
    """Extract and format text content from a parsed JSONL message.

    Produces a clean, human-readable line for display in the task progress stream.
    Returns an empty string for messages with no meaningful content (bare type-only
    messages, tool_result blocks without text, etc.) so callers can skip them.
    """
    msg_type = msg.get("type", "unknown")

    # Extract message content blocks
    text_parts = []
    if "message" in msg and "content" in msg["message"]:
        content = msg["message"]["content"]

        # Handle string content (direct text from Claude Code session)
        if isinstance(content, str):
            text = content.strip()
            if text and len(text) > 0:
                if len(text) > 200:
                    text = text[:200].rstrip() + "..."
                text_parts.append(text)
        # Handle array of content blocks (Claude API format)
        elif isinstance(content, list):
            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text:
                        # Truncate very long text blocks — they're assistant narration, not output
                        if len(text) > 200:
                            text = text[:200].rstrip() + "..."
                        text_parts.append(text)
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    # Format tool input as a brief label where possible
                    inp = block.get("input") or {}
                    if tool_name in ("Bash", "bash") and inp.get("command"):
                        cmd = str(inp["command"]).strip()
                        brief = (cmd[:80] + "...") if len(cmd) > 80 else cmd
                        text_parts.append(f"$ {brief}")
                    elif tool_name in ("Read", "read", "Write", "write") and inp.get("file_path"):
                        text_parts.append(f"{tool_name}: {inp['file_path']}")
                    elif tool_name in ("Edit", "edit") and inp.get("file_path"):
                        text_parts.append(f"Edit: {inp['file_path']}")
                    elif tool_name in ("WebSearch", "WebFetch") and (inp.get("query") or inp.get("url")):
                        target = inp.get("query") or inp.get("url", "")
                        brief = (target[:80] + "...") if len(target) > 80 else target
                        text_parts.append(f"{tool_name}: {brief}")
                    else:
                        text_parts.append(f"Using {tool_name}")
                elif block_type == "tool_result":
                    # Tool results are noisy; skip them (the tool_use line is enough context)
                    pass
                elif block_type == "thinking":
                    think = block.get("thinking", "").strip()
                    if think:
                        brief = (think[:120] + "...") if len(think) > 120 else think
                        text_parts.append(f"[thinking] {brief}")

    # Only emit assistant turns with real content; skip bare user/tool_result lines
    if msg_type == "assistant" and text_parts:
        return " ".join(text_parts)
    if msg_type not in ("assistant", "user") and text_parts:
        return " ".join(text_parts)
    return ""


def _detect_new_session(
    project_dir: Path,
    cutoff_time: float,
    timeout: float = 15.0,
) -> Optional[str]:
    """Detect a new JSONL session file created after cutoff_time.

    Returns the session ID (filename stem) or None if timeout.
    """
    start = time.time()
    _log.info("waiting for new session file in %s (timeout %.0fs)", project_dir, timeout)
    while time.time() - start < timeout:
        try:
            files = list(project_dir.glob("*.jsonl"))
            _log.debug("  found %d jsonl files in project dir", len(files))
            for jsonl_file in files:
                try:
                    mtime = jsonl_file.stat().st_mtime
                    age = time.time() - mtime
                    if mtime > cutoff_time:
                        session_id = jsonl_file.stem
                        _log.info("✓ detected new claude session: %s (age %.1fs)", session_id, age)
                        return session_id
                    else:
                        _log.debug("  file %s is too old (%.1fs)", jsonl_file.name, age)
                except Exception as e:
                    _log.debug("error checking file: %s", e)
        except Exception as e:
            _log.debug("error globbing: %s", e)
        time.sleep(0.5)
    _log.warning("✗ timeout waiting for new session after %.0fs", timeout)
    return None


def _monitor_claude_session(
    working_dir: str,
    callback: Callable[[ProgressEvent], None],
    stop_event: threading.Event,
    cutoff_time: Optional[float] = None,
    poll_interval: float = 0.1,
    max_wait_seconds: float = 30.0,
) -> None:
    """Monitor Claude Code session files and stream updates via callback.

    Runs in a background thread. Polls for new JSONL lines and emits ProgressEvents
    as Claude Code produces output (thinking, tool calls, text, etc.).
    """
    _log.info("monitor thread started for working_dir: %s", working_dir)
    if cutoff_time is None:
        cutoff_time = time.time()
    try:
        project_dir = _find_claude_project_dir(working_dir)
        if not project_dir:
            _log.warning("✗ project dir not found: checked %s and QAR_DEEP_WORKING_DIR/QAR_CORPUS_ROOT",
                        working_dir)
            _log.info("waiting for .claude/projects to be created (%.0fs timeout)...", max_wait_seconds)
            # Wait for it to be created, then find it
            start = time.time()
            while time.time() - start < max_wait_seconds and not stop_event.is_set():
                time.sleep(poll_interval)
                project_dir = _find_claude_project_dir(working_dir)
                if project_dir:
                    _log.info("✓ .claude/projects created: %s", project_dir)
                    break

        if not project_dir:
            _log.error("✗ FATAL: could not locate .claude/projects dir after %.1f seconds", max_wait_seconds)
            return

        _log.info("✓ found claude project dir: %s", project_dir)
        processed_lines: Set[str] = set()
        event_count = 0

        # Monitor ALL new session files (parallel tasks create multiple sessions)
        _log.info("monitoring for new claude sessions (parallel tasks may create multiple)...")
        watched_files: Dict[str, int] = {}  # filename -> last known file size
        session_timeout = 15.0
        session_start = time.time()

        while not stop_event.is_set():
            # Periodically check for new session files
            try:
                # Find all JSONL files in the project dir and subdirs
                for jsonl_file in project_dir.rglob("*.jsonl"):
                    try:
                        mtime = jsonl_file.stat().st_mtime
                        # Only monitor files created after we started
                        if mtime < cutoff_time:
                            continue

                        if jsonl_file not in watched_files:
                            _log.info("detected new session: %s", jsonl_file.name)
                            watched_files[jsonl_file] = 0  # Start watching from beginning

                        # Read new lines from this session file
                        current_size = jsonl_file.stat().st_size
                        if current_size > watched_files[jsonl_file]:
                            try:
                                with open(jsonl_file, 'r') as f:
                                    f.seek(watched_files[jsonl_file])
                                    for line in f:
                                        line_hash = _hash_line(line)
                                        if line_hash in processed_lines:
                                            continue

                                        try:
                                            msg = json.loads(line.strip())
                                            msg_type = msg.get("type", "")

                                            # Only emit assistant messages (the actual work output)
                                            if msg_type not in ("assistant", "message"):
                                                processed_lines.add(line_hash)
                                                continue

                                            processed_lines.add(line_hash)

                                            # Format and emit the message as a progress event
                                            msg_text = _format_message_text(msg)
                                            if msg_text and msg_text.strip():
                                                event_count += 1
                                                _log.info("emitting exec event #%d from %s: %s",
                                                         event_count, jsonl_file.name, msg_text[:80])
                                                callback(ProgressEvent(
                                                    type=EVENT_EXEC,
                                                    text=msg_text,
                                                    data={"message_type": msg_type, "file": jsonl_file.name}
                                                ))
                                        except json.JSONDecodeError:
                                            pass

                                watched_files[jsonl_file] = current_size
                            except FileNotFoundError:
                                pass
                    except Exception as e:
                        _log.debug("error processing session file %s: %s", jsonl_file.name, e)

            except Exception as e:
                _log.debug("error scanning project dir: %s", e)

            # Stop monitoring after timeout with no new activity
            if time.time() - session_start > session_timeout and not watched_files:
                _log.warning("timeout waiting for any new sessions")
                break

            time.sleep(poll_interval)

    except Exception as e:
        _log.warning("claude session monitor failed: %s", e)


class SubprocessGoalRunner(DeepRunner):
    """Reference DeepRunner: spawn Claude Code headless with ``/goal`` + ``--max-turns``.

    The working dir, binary, model, and any context preamble are CONFIG, not hardcoded
    paths. Exit code 0 = goal met cleanly; non-zero =
    hit the turn/budget limit or errored (DeepResult.met=False with a clear message).

    When an emit callback is provided, streams live Claude Code session updates to the
    caller so they see what Claude is doing in real-time.
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
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
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
        cmd: List[str] = [binary, "-p", "--output-format", "json"]
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

        # Start monitoring Claude Code session in a background thread if emit is provided.
        stop_monitor = threading.Event()
        monitor_thread = None
        cutoff_time = time.time()  # Sessions created after this are new
        if emit is not None:
            _log.info("starting claude session monitor for deep run in: %s",
                     self.cfg.working_dir)
            monitor_thread = threading.Thread(
                target=_monitor_claude_session,
                args=(self.cfg.working_dir, emit, stop_monitor, cutoff_time),
                daemon=True
            )
            monitor_thread.start()
        else:
            _log.warning("emit callback not provided; NOT starting session monitor (no live output)")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cfg.working_dir,
                env=self._build_env(),
            )
        except FileNotFoundError:
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=1)
            return DeepResult(met=False, error=f"worker binary not found: {self.cfg.claude_path}")
        except PermissionError as e:
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=1)
            return DeepResult(met=False, error=f"permission denied running worker: {e}")

        # Communicate with the process (send prompt and wait for completion)
        try:
            raw, err = proc.communicate(
                input=prompt.encode("utf-8"),
                timeout=self.cfg.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=1)
            return DeepResult(met=False, error="goal run exceeded the time limit before completing")
        finally:
            # Stop monitoring thread
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2)

        raw = raw.decode("utf-8", errors="replace") if raw else ""
        err = err.decode("utf-8", errors="replace") if err else None
        # We requested ``--output-format json``: parse the worker's final result text AND its
        # reported token usage / cost out of the JSON envelope. ``out`` becomes the human-readable
        # result; ``tokens``/``cost`` feed the goal loop's overall token budget. If parsing fails
        # (older worker, plain text), fall back to treating stdout as the result with no usage.
        out, tokens, cost, json_is_error = _parse_worker_output(raw)

        # The escalation-marker contract: the worker raised a human decision mid-run and printed
        # ``QAR-ESCALATED: <decision_id>``. That overrides met-vs-limit — the run is PAUSED on a
        # human, so the executor must report needs_you with the decision linked, regardless of
        # the exit code.
        decision_id = extract_escalation_id(out)
        if decision_id:
            return DeepResult(met=False, output=out, decision_id=decision_id,
                              tokens=tokens, cost_usd=cost)
        if proc.returncode == 0 and not json_is_error:
            # Safety net against a SILENT NO-OP: a real headless ``-p`` run always prints its final
            # result, so exit-0 with EMPTY output means the worker never actually ran the goal (the
            # failure mode when ``-p`` is missing, or the binary mis-launches). Report it as a
            # failure with a clear message instead of a hollow "met" that shows "Completed" but did
            # nothing. (A pure chit-chat run has an empty ``goal`` and is exempt.)
            if goal.strip() and not out.strip():
                return DeepResult(
                    met=False, output=out, tokens=tokens, cost_usd=cost,
                    error="worker exited cleanly but produced NO output — the goal did not actually "
                          "run (check that the worker runs headless, e.g. Claude Code needs -p).")
            return DeepResult(met=True, output=out, tokens=tokens, cost_usd=cost)
        if not err:
            err = (f"Goal run did not complete cleanly (exit {proc.returncode}) — likely hit the "
                   "turn/budget limit before fully meeting the goal.")
        return DeepResult(met=False, output=out, error=err, tokens=tokens, cost_usd=cost)
