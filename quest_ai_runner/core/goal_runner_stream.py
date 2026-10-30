"""Persistent stream-json deep runner: one live Claude Code session per run that can be steered.

``SubprocessGoalRunner`` starts ``claude -p`` once, hands it the whole prompt and waits for the
process to exit, so nothing can reach the model while it works. This runner starts
``claude -p --input-format stream-json --output-format stream-json`` and keeps the session open for
the life of the run, so the caller can:

  * STEER: a message the person sends while the worker runs is written to the session's stdin and
    the model reads it at its next step (in the same turn, as soon as the tool it is running
    finishes). The message is CLAIMED from the caller's ``RunControl`` only at the moment it is
    written, so it arrives exactly once and only to this run.
  * STOP: an ``interrupt`` control request stops the step that is running and keeps the session;
    the process is then released. A process that ignores the interrupt is terminated, then killed,
    after a grace period.
  * PAUSE: the same interrupt, ending the run as ``paused`` with its session id; the work resumes
    later with ``--resume`` on the same thread.

It keeps what the one-shot runner already does: the live progress stream (the session JSONL
monitor), ``--resume`` (including the missing-session retry), the subscription login (the same
environment, no API key), the usage-limit wait, the empty-output guard, the liveness review and the
wall-clock cap. It is a SECOND implementation of ``run-deep-work``; the one-shot runner stays the
selected one until the metric ``receive-input-mid-run`` shows this one wins.

A Claude Code older than ``MIN_STREAM_CONTROL_VERSION`` cannot report an interrupt, so this runner
hands the run to the one-shot runner instead of pretending (nothing is steerable there).

Exactly-once delivery holds by construction: ONE loop thread decides everything. It claims messages,
writes them, reads the model's replay of them, and decides the run is over. A message can therefore
never be claimed after the decision to finish, and nothing it wrote is dropped without being
reported (``DeepResult.unseen_inputs``).
"""
from __future__ import annotations

import json
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from esoi import implementation

from . import usage_limit
from .deep_launch import build_deep_argv
from hubofhubs.ports.deep import DeepResult, LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED, ProgressEvent
from hubofhubs.ports.events import EVENT_EXEC
from .goal_runner import SubprocessGoalRunner
from .goal_runner_contract import (
    BALANCED_CLI_MODEL,
    CANCEL_POLL_SECONDS,
    DEFAULT_DEEP_MAX_TURNS,
    SubprocessConfig,
    _log,
    _parse_worker_output,
    cli_safe_model,
    compose_goal_prompt_fitted,
    envelope_reports_no_work,
    extract_escalation_id,
    kill_process_group,
    resolve_deep_review_interval_seconds,
    resolve_deep_timeout_seconds,
    resume_target_missing,
    signal_process_group,
)
from .goal_runner_session import (
    _monitor_claude_session,
    describe_session_activity,
    resolve_session_file,
    resolved_model_from_session,
    session_file_size,
    worker_output_is_thin,
)
from hubofhubs.ports.control import RunControl

# The first Claude Code release whose stream-json session answers an ``interrupt`` control request
# with a receipt. Older releases are handed to the one-shot runner.
MIN_STREAM_CONTROL_VERSION = (2, 1, 219)

# How long the loop waits for an event before it looks at the clock, in seconds.
LOOP_TICK_SECONDS = 0.1

# After a turn ends with a message written but not yet replayed, how long to wait for the replay
# before that message is reported as unseen. A replay that never comes (an older CLI that ignores
# the replay flag) must end the run, not hang it.
DEFAULT_ECHO_WAIT_SECONDS = 10.0

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
VERSION_CACHE: Dict[str, Optional[Tuple[int, int, int]]] = {}


def cli_version(binary: str, env: Dict[str, str]) -> Optional[Tuple[int, int, int]]:
    """The installed Claude Code version as a tuple, cached per binary; None when it cannot be read."""
    if binary in VERSION_CACHE:
        return VERSION_CACHE[binary]
    found: Optional[Tuple[int, int, int]] = None
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, timeout=15, env=env)
        m = VERSION_RE.search(done.stdout.decode("utf-8", errors="replace"))
        if m:
            found = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except FileNotFoundError:
        # Let the launch itself report a missing binary, with the usual message.
        found = (999, 0, 0)
    except Exception:  # noqa: BLE001 -- an unreadable version means "do not assume support"
        found = None
    VERSION_CACHE[binary] = found
    return found


def user_line(text: str) -> str:
    """One stream-json user message, as a line of stdin."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def interrupt_line(request_id: str) -> str:
    """The control request that stops the running step and keeps the session."""
    return json.dumps({"type": "control_request", "request_id": request_id,
                       "request": {"subtype": "interrupt"}}) + "\n"


def echo_text(event: Dict[str, Any]) -> Optional[str]:
    """The text of a replayed USER message, or None for any other ``user`` event (a tool result)."""
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [str(b.get("text") or "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


@dataclass
class Sent:
    """A steering message written to the session, and when the model's replay of it came back."""
    id: str
    text: str
    written_at: float
    seen_at: Optional[float] = None


class StreamRun:
    """One live stream-json session, driven by ONE loop thread.

    ``drive`` returns a ``Finish`` describing how the session ended; turning that into a
    ``DeepResult`` (met-vs-limit, usage limit, escalation) is the runner's job.
    """

    def __init__(self, proc: "subprocess.Popen", prompt: str, control: Optional[RunControl], *,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 grace_seconds: float, echo_wait_seconds: float,
                 timeout_seconds: Optional[float], review: Optional[Callable[[float], Tuple[bool, str]]],
                 review_interval: Optional[float], clock: Callable[[], float] = time.monotonic) -> None:
        self.proc = proc
        self.prompt = prompt
        self.control = control
        self.cancel_check = cancel_check
        self.grace = grace_seconds
        self.echo_wait = echo_wait_seconds
        self.timeout = timeout_seconds
        self.review = review
        self.review_interval = review_interval
        self.clock = clock
        self.events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.stderr_parts: List[str] = []
        self.sent: List[Sent] = []
        self.results: List[Dict[str, Any]] = []
        self.session_id: Optional[str] = None
        self.last_text = ""
        self.finished = False
        self.stopping = False
        self.stdin_open = True
        self.interrupt_id: Optional[str] = None
        self.interrupt_acked = False
        self.result_after_interrupt = False
        self.eof = False
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()

    # --- reading -------------------------------------------------------------------------

    def read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.events.put(event)
        except Exception:  # noqa: BLE001 -- a closed pipe ends the stream
            pass
        finally:
            self.events.put(None)

    def read_stderr(self) -> None:
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                self.stderr_parts.append(line)
        except Exception:  # noqa: BLE001
            pass

    # --- writing -------------------------------------------------------------------------

    def write(self, line: str) -> bool:
        if not self.stdin_open:
            return False
        try:
            self.proc.stdin.write(line)  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
            return True
        except Exception:  # noqa: BLE001 -- the process is gone
            self.stdin_open = False
            return False

    def close_stdin(self) -> None:
        if self.stdin_open:
            self.stdin_open = False
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    # --- steering ------------------------------------------------------------------------

    def steer(self) -> None:
        """Claim the messages waiting for this run and write each into the session.

        Called only from the loop thread and only while the run is still going, so a claimed
        message is always one this run writes. Each is recorded BEFORE it is written: if the
        write fails the message stays on the unseen list instead of vanishing."""
        if self.control is None or self.finished or self.stopping:
            return
        try:
            messages = self.control.claim_inputs() or []
        except Exception:  # noqa: BLE001 -- a broken claim must never end a run
            return
        for message in messages:
            if not str(message.text or "").strip():
                continue
            record = Sent(id=message.id, text=message.text, written_at=self.clock())
            self.sent.append(record)
            self.write(user_line(message.text))

    def mark_replayed(self, text: str) -> None:
        """The model's replay of a user message came back: those steering messages are SEEN."""
        seen_ids: List[str] = []
        body = text.strip()
        if body == self.prompt.strip():
            return  # the replay of the opening prompt is not a steering message
        waiting = [r for r in self.sent if r.seen_at is None and r.text.strip()]
        # An exact replay marks ONE message (two identical messages are two replays); a replay that
        # merged several messages into one turn marks every message it contains.
        exact = next((r for r in waiting if r.text.strip() == body), None)
        for record in ([exact] if exact is not None else [r for r in waiting if r.text.strip() in body]):
            record.seen_at = self.clock()
            seen_ids.append(record.id)
        if seen_ids and self.control is not None:
            try:
                self.control.mark_seen(seen_ids, "deep_worker")
            except Exception:  # noqa: BLE001 -- a status stamp never affects the run
                pass

    def unseen(self) -> List[str]:
        return [r.text for r in self.sent if r.seen_at is None]

    # --- events --------------------------------------------------------------------------

    def handle(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = str(event.get("session_id") or "") or self.session_id
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    self.last_text = str(block["text"])
        elif kind == "user":
            text = echo_text(event)
            if text is not None:
                self.mark_replayed(text)
        elif kind == "control_response":
            response = event.get("response") or {}
            if response.get("request_id") == self.interrupt_id or event.get("request_id") == self.interrupt_id:
                self.interrupt_acked = True
        elif kind == "result":
            self.results.append(event)
            if self.interrupt_id is not None:
                self.result_after_interrupt = True

    # --- the loop ------------------------------------------------------------------------

    def drive(self) -> Tuple[str, str]:
        """Run the session to its end. Returns ``(how, detail)``:

        ``("done", "")`` the model finished and nothing it was given is waiting;
        ``("cancel" | "pause", how_stopped)`` stopped on request (``how_stopped`` is
        ``interrupted``, ``terminated`` or ``killed``);
        ``("timeout" | "review", reason)`` ended by the wall-clock cap or the liveness review;
        ``("exited", "")`` the process ended by itself without a final result."""
        self.write(user_line(self.prompt))
        started = self.clock()
        poll = max(0.05, self.control.poll_seconds) if self.control is not None else CANCEL_POLL_SECONDS
        next_poll = started + poll
        next_review = started + self.review_interval if self.review_interval else None
        echo_deadline: Optional[float] = None
        while True:
            event: Optional[Dict[str, Any]] = None
            try:
                event = self.events.get(timeout=LOOP_TICK_SECONDS)
                if event is None:
                    self.eof = True
                    return "exited", ""
            except queue.Empty:
                pass
            if event is not None:
                self.handle(event)
                if event.get("type") == "result":
                    if not [r for r in self.sent if r.seen_at is None]:
                        self.finished = True
                        return "done", ""
                    echo_deadline = self.clock() + self.echo_wait
            now = self.clock()
            # A result left a written message whose replay has not come back. Its replay ends the
            # wait (the turn it starts ends the run); a replay that never comes ends it here.
            if echo_deadline is not None and now >= echo_deadline \
                    and [r for r in self.sent if r.seen_at is None]:
                self.finished = True
                return "done", ""
            if now >= next_poll:
                next_poll = now + poll
                wanted = self.stop_wanted()
                if wanted:
                    return wanted, self.stop(wanted)
                self.steer()
            elapsed = now - started
            if self.timeout is not None and elapsed >= self.timeout:
                kill_process_group(self.proc)   # a capped run is not asked politely
                return "timeout", f"Deep run exceeded its configured wall-clock timeout: ran for " \
                                  f"{elapsed:.0f}s against a {self.timeout:.0f}s limit."
            if next_review is not None and now >= next_review and self.review is not None:
                next_review = now + (self.review_interval or 0)
                stop_now, reason = self.review(elapsed)
                if stop_now:
                    kill_process_group(self.proc)
                    return "review", reason

    def stop_wanted(self) -> str:
        for check, how in ((self.control.pause_requested if self.control else None, "pause"),
                           (self.control.cancel_requested if self.control else None, "cancel"),
                           (self.cancel_check, "cancel")):
            if check is None:
                continue
            try:
                if check():
                    return how
            except Exception:  # noqa: BLE001 -- a broken check never ends a run
                continue
        return ""

    # --- stopping ------------------------------------------------------------------------

    def stop(self, reason: str) -> str:
        """Interrupt the running step, keep the session, then release the process.

        Returns ``interrupted`` when the worker took the interrupt and exited when its input
        closed, ``terminated`` when it had to be sent SIGTERM, ``killed`` when it had to be killed.
        No message is claimed from this point on."""
        self.stopping = True
        self.interrupt_id = f"int-{uuid.uuid4().hex[:12]}"
        self.write(interrupt_line(self.interrupt_id))
        deadline = self.clock() + self.grace
        while self.clock() < deadline and not self.result_after_interrupt and not self.eof:
            try:
                event = self.events.get(timeout=LOOP_TICK_SECONDS)
            except queue.Empty:
                continue
            if event is None:
                self.eof = True
                break
            self.handle(event)
        if self.result_after_interrupt or self.eof:
            # The step ended and the session is intact. Closing input lets the process exit.
            self.close_stdin()
            if self.wait_exit(self.grace):
                return "interrupted"
        else:
            self.close_stdin()
        signal_process_group(self.proc, signal.SIGTERM)
        if self.wait_exit(self.grace):
            return "terminated"
        kill_process_group(self.proc)
        self.wait_exit(5)
        return "killed"

    def wait_exit(self, seconds: float) -> bool:
        try:
            self.proc.wait(timeout=max(0.0, seconds))
            return True
        except subprocess.TimeoutExpired:
            return False

    def release(self) -> None:
        """Close input and make sure the process is gone. Safe to call after any ending."""
        self.close_stdin()
        if not self.wait_exit(self.grace):
            signal_process_group(self.proc, signal.SIGTERM)
            if not self.wait_exit(self.grace):
                kill_process_group(self.proc)
                self.wait_exit(5)


@implementation("run-deep-work", "stream_json")
class StreamJsonGoalRunner(SubprocessGoalRunner):
    """Deep runner holding one live ``claude -p --input-format stream-json`` session per run.

    Built from the same ``SubprocessConfig`` as the one-shot runner (working directory, binary,
    tools, timeouts, grace for a stop). The extra per-call argument is ``control``, a
    ``hubofhubs.ports.control.RunControl``; without one the run is the same single prompt, only over a
    session that is closed when the model finishes."""

    config_class = SubprocessConfig

    # Class attributes so a test or a consumer can tune them without a new config field.
    echo_wait_seconds = DEFAULT_ECHO_WAIT_SECONDS

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None,
                 working_dir: Optional[str] = None,
                 resume_session_id: Optional[str] = None,
                 requester_user_id: Optional[str] = None,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 control: Optional[RunControl] = None) -> DeepResult:
        """Run the goal over a live session. Never raises (the DeepRunner port)."""
        landed_already = self.landed_in(working_dir, resume_session_id)
        try:
            res = self.run_goal_once(goal=goal, brief=brief, model=model, max_turns=max_turns,
                                     emit=emit, context_preamble=context_preamble, run_id=run_id,
                                     working_dir=working_dir, resume_session_id=resume_session_id,
                                     requester_user_id=requester_user_id,
                                     cancel_check=cancel_check, control=control)
        except Exception as e:  # noqa: BLE001
            _log.error("stream deep worker failed unexpectedly: %s: %s", type(e).__name__, e,
                       exc_info=True)
            return DeepResult(met=False, error=f"The deep worker failed unexpectedly: {type(e).__name__}: {e}")
        if res.resolved_model is None and res.session_id:
            res.resolved_model = resolved_model_from_session(
                self.cfg.working_dir if working_dir is None else working_dir, res.session_id)
        self.count_landed(res, working_dir, landed_already)
        return res

    def run_goal_once(self, *, goal: str, brief: str, model: Optional[str] = None,  # type: ignore[override]
                      max_turns: Optional[int] = None,
                      emit: Optional[Callable[[ProgressEvent], None]] = None,
                      context_preamble: Optional[str] = None,
                      run_id: Optional[str] = None,
                      working_dir: Optional[str] = None,
                      resume_session_id: Optional[str] = None,
                      prompt_budget_tokens: Optional[int] = None,
                      requester_user_id: Optional[str] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      control: Optional[RunControl] = None) -> DeepResult:
        env = self._build_env(requester_user_id, run_id)
        binary = self.cfg.claude_path
        version = cli_version(binary, env)
        if version is None or version < MIN_STREAM_CONTROL_VERSION:
            _log.warning("claude %s cannot report an interrupt; running this goal one-shot instead",
                         version)
            # The parent's once-method directly: its ``run_goal`` would call back into this class.
            return SubprocessGoalRunner.run_goal_once(
                self, goal=goal, brief=brief, model=model, max_turns=max_turns,
                emit=emit, context_preamble=context_preamble, run_id=run_id,
                working_dir=working_dir, resume_session_id=resume_session_id,
                prompt_budget_tokens=prompt_budget_tokens,
                requester_user_id=requester_user_id, cancel_check=cancel_check)
        paused_on = usage_limit.active_limit()
        if paused_on is not None:
            return DeepResult(met=False, error=f"Claude Code usage limit: {paused_on.message}",
                              session_id=resume_session_id, usage_limited=True)
        from . import prompt_budget
        preamble = self.cfg.context_preamble if context_preamble is None else context_preamble
        window_model = cli_safe_model(model, self._model_translator) or model
        budget = prompt_budget.resolve_deep_prompt_budget(
            prompt_budget_tokens or self.cfg.prompt_token_budget, model=window_model)
        prompt, fit = compose_goal_prompt_fitted(goal, brief, preamble=preamble,
                                                 budget_tokens=budget, model=window_model)
        prompt_tokens = prompt_budget.estimate_tokens(prompt)
        effective_dir = self.cfg.working_dir if working_dir is None else working_dir
        if resume_session_id:
            from .goal_runner_session import session_replay_tokens
            replay = session_replay_tokens(effective_dir, resume_session_id)
            room = prompt_budget.max_prompt_tokens_for(window_model)
            if replay is not None and replay + prompt_tokens > room:
                resume_session_id = None

        turns = max_turns if max_turns is not None else DEFAULT_DEEP_MAX_TURNS
        if resume_session_id:
            session_id, session_flag = resume_session_id, "--resume"
        else:
            session_id, session_flag = str(uuid.uuid4()), "--session-id"
        cmd = build_deep_argv(
            binary, streaming=True, skip_permissions=self.cfg.skip_permissions,
            allowed_tools=self.cfg.allowed_tools, disallowed_tools=self.cfg.disallowed_tools,
            mcp_config_path=self.cfg.mcp_config_path,
            model=cli_safe_model(model, self._model_translator) or BALANCED_CLI_MODEL, turns=turns,
            goal_given=bool(goal.strip()), session_flag=session_flag, session_id=session_id,
            launch=self._launch_settings())

        stop_monitor = threading.Event()
        monitor_thread = None
        if emit is not None:
            # Read before the worker starts: a resumed session already holds the output of its
            # earlier spawn, and replaying it would show the person everything a second time.
            already_reported = session_file_size(effective_dir, session_id) if resume_session_id else 0
            monitor_thread = threading.Thread(
                target=_monitor_claude_session,
                args=(effective_dir, emit, stop_monitor, time.time()),
                kwargs={"forced_run_id": run_id, "session_id": session_id,
                        "start_offset": already_reported}, daemon=True)
            monitor_thread.start()
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=effective_dir, env=env, start_new_session=True, text=True, bufsize=1,
                encoding="utf-8", errors="replace")
        except FileNotFoundError:
            stop_monitor.set()
            return DeepResult(met=False, launch_failed=True, launch_problem=LAUNCH_MISSING_PROGRAM,
                              error=f"The deep worker could not start: binary not found ({binary}).")
        except PermissionError as e:
            stop_monitor.set()
            return DeepResult(met=False, launch_failed=True, launch_problem=LAUNCH_PERMISSION_DENIED,
                              error=f"The deep worker could not start: permission denied ({e}).")

        started = time.time()
        review_limit: Dict[str, Any] = {}   # per call: one runner serves many concurrent runs

        def review(elapsed: float) -> Tuple[bool, str]:
            stop_now, reason, limit = self._review_liveness(effective_dir, session_id, elapsed, emit, run_id)
            review_limit["limit"] = limit
            return stop_now, reason

        run = StreamRun(
            proc, prompt, control, cancel_check=cancel_check,
            grace_seconds=self.cfg.cancel_grace_seconds, echo_wait_seconds=self.echo_wait_seconds,
            timeout_seconds=resolve_deep_timeout_seconds(self.cfg.timeout_seconds),
            review=review,
            review_interval=resolve_deep_review_interval_seconds(self.cfg.review_interval_seconds))
        try:
            how, detail = run.drive()
        finally:
            run.release()
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2)
        _log.info("stream deep run %s ended: %s %s (%.1fs)", session_id, how, detail, time.time() - started)
        unseen = run.unseen()
        final = run.results[-1] if run.results else None
        raw = json.dumps(final) if final else ""
        out, tokens, cost, json_is_error, subtype = _parse_worker_output(raw)
        err = "".join(run.stderr_parts).strip() or None

        if how in ("cancel", "pause"):
            paused = how == "pause"
            return DeepResult(
                met=False, output=out or run.last_text, tokens=tokens, cost_usd=cost,
                session_id=session_id, cancelled=True, paused=paused, unseen_inputs=unseen,
                error=("The run was paused on request. Changes it had already finished are kept; "
                       "resume it to continue from the same session." if paused else
                       "The run was stopped on request. Changes it had already finished are kept; "
                       "a step it was in the middle of may not have completed."))
        if how == "timeout":
            return DeepResult(met=False, session_id=session_id, unseen_inputs=unseen,
                              error=f"{detail} The worker process group was killed.")
        if how == "review":
            limit = review_limit.get("limit")
            if limit is not None:
                limit = usage_limit.record(limit)
                return DeepResult(met=False, output="", session_id=session_id, usage_limited=True,
                                  unseen_inputs=unseen, error=f"Claude Code usage limit: {limit.message}")
            activity = describe_session_activity(effective_dir, session_id)
            return DeepResult(
                met=False, session_id=session_id, unseen_inputs=unseen,
                error=(f"Stopped by the liveness review: {detail} The worker process group was "
                       "killed. This is a hard failure, not a silent success, even if partial output "
                       "exists." + (f"\n\n{activity}" if activity else "")))

        returncode = proc.returncode if proc.returncode is not None else 0
        if resume_session_id and resume_target_missing(returncode, out, err):
            if emit is not None:
                emit(ProgressEvent(type=EVENT_EXEC,
                                   text="The earlier session is no longer available, so this run starts fresh.",
                                   data={"run_id": run_id, "phase": "resume_unavailable"}))
            return self.run_goal(goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit,
                                 context_preamble=context_preamble, run_id=run_id,
                                 working_dir=working_dir, resume_session_id=None,
                                 requester_user_id=requester_user_id, cancel_check=cancel_check,
                                 control=control)
        if returncode != 0 or json_is_error:
            limit = (usage_limit.limit_from_session_file(resolve_session_file(effective_dir, session_id))
                     or usage_limit.detect_usage_limit(out) or usage_limit.detect_usage_limit(err or ""))
            if limit is not None:
                limit = usage_limit.record(limit)
                return DeepResult(met=False, output="", tokens=tokens, cost_usd=cost,
                                  session_id=session_id, usage_limited=True, unseen_inputs=unseen,
                                  error=f"Claude Code usage limit: {limit.message}")
        decision_id = extract_escalation_id(out)
        if decision_id:
            return DeepResult(met=False, output=out, decision_id=decision_id, tokens=tokens,
                              cost_usd=cost, session_id=session_id, unseen_inputs=unseen)
        if final is not None and returncode == 0 and not json_is_error:
            if goal.strip() and not out.strip():
                return DeepResult(
                    met=False, output=out, tokens=tokens, cost_usd=cost, session_id=session_id,
                    unseen_inputs=unseen,
                    error="worker exited cleanly but produced NO output, so the goal did not "
                          "actually run (check that the worker runs headless, e.g. Claude Code "
                          "needs -p).")
            return DeepResult(met=True, output=out, tokens=tokens, cost_usd=cost,
                              session_id=session_id, unseen_inputs=unseen)
        if final is not None and envelope_reports_no_work(raw):
            reason = (out or "").strip() or (err or "").strip() or f"exit {returncode}"
            if prompt_budget.is_prompt_too_long(reason):
                if resume_session_id:
                    return self.run_goal_once(
                        goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit,
                        context_preamble=context_preamble, run_id=run_id, working_dir=working_dir,
                        resume_session_id=None, prompt_budget_tokens=budget,
                        requester_user_id=requester_user_id, cancel_check=cancel_check,
                        control=control)
                tighter = budget // 2
                if tighter >= prompt_budget.MIN_DEEP_PROMPT_TOKEN_BUDGET:
                    return self.run_goal_once(
                        goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit,
                        context_preamble=context_preamble, run_id=run_id, working_dir=working_dir,
                        resume_session_id=None, prompt_budget_tokens=tighter,
                        requester_user_id=requester_user_id, cancel_check=cancel_check,
                        control=control)
            return DeepResult(met=False, output="", session_id=session_id, launch_failed=True,
                              unseen_inputs=unseen,
                              error=f"The deep worker could not start: {reason[:600]}")
        # No final result, or one that reports an error after real work.
        tail = (out or run.last_text or "").strip()
        if final is None:
            message = (f"The worker exited {returncode} before it reported a result. The goal was not "
                       "confirmed met.")
            if err:
                message = f"{message}\n\nIts error output:\n{err[-1500:]}"
        elif subtype == "error_max_turns":
            message = (f"The worker used its entire {int(turns)}-turn budget without declaring the "
                       "goal met, so the goal is UNCONFIRMED rather than failed. Whatever it "
                       "finished before the budget ran out still happened; read the run output "
                       "below to see how far it got.")
        else:
            message = (f"The worker ended with an error ({subtype or 'unknown'}). The goal was not "
                       "confirmed met. Read the run output below for what it actually did.")
        if tail:
            message = f"{message}\n\nLast output:\n{tail[-1500:]}"
        if worker_output_is_thin(tail):
            activity = describe_session_activity(effective_dir, session_id, worker_output=tail)
            if activity:
                message = f"{message}\n\n{activity}"
        return DeepResult(met=False, output=out or run.last_text, error=message, tokens=tokens,
                          cost_usd=cost, session_id=session_id, unseen_inputs=unseen,
                          limit_hit=(subtype == "error_max_turns"))
