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
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

from esoi import implementation

from . import usage_limit
from .deep_launch import DeepLaunchSettings, build_deep_argv
from .requester import apply_requester_env
from .runtime_settings import process_environment
from hubofhubs.ports.deep import DeepResult, DeepRunner, LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED, ProgressEvent
from hubofhubs.ports.events import EVENT_EXEC

from .goal_runner_contract import (  # noqa: F401
    BALANCED_CLI_MODEL,
    CANCEL_POLL_SECONDS,
    DEEP_REVIEW_CALL_TIMEOUT_SECONDS,
    DEEP_REVIEW_PROMPT,
    DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    ESCALATION_MARKER,
    GoalRunner,
    QAR_DEEP_TIMEOUT_SECONDS_ENV_VAR,
    SubprocessConfig,
    WEB_TOOLS,
    _is_claude_model,
    _log,
    _parse_worker_output,
    _run_goal_accepts_context_preamble,
    cli_safe_model,
    set_cli_model_translator,
    compose_goal_prompt,
    compose_goal_prompt_fitted,
    envelope_reports_no_work,
    extract_escalation_id,
    kill_process_group,
    stop_process_group,
    parse_deep_review,
    resolve_deep_review_interval_seconds,
    resolve_deep_launch,
    resolve_deep_timeout_seconds,
    resume_target_missing,
)
from .goal_runner_session import (  # noqa: F401
    _find_claude_project_dir,
    _find_session_file,
    _monitor_claude_session,
    describe_session_activity,
    landed_file_changes,
    read_session_activity_tail,
    resolve_session_file,
    resolved_model_from_session,
    session_replay_tokens,
    subagent_work_in_flight,
    worker_output_is_thin,
)


@implementation("run-deep-work", "subprocess")
class SubprocessGoalRunner(DeepRunner):
    """Reference DeepRunner: spawn Claude Code headless with ``/goal`` + ``--max-turns``.

    The working dir, binary, model, and any context preamble are CONFIG, not hardcoded
    paths. Exit code 0 = goal met cleanly; non-zero =
    hit the turn/budget limit or errored (DeepResult.met=False with a clear message).

    When an emit callback is provided, streams live Claude Code session updates to the
    caller so they see what Claude is doing in real-time.
    """

    config_class = SubprocessConfig   # the dataclass this runner is built from (core.resolve.resolve_config)

    def __init__(self, config: SubprocessConfig, *,
                 model_translator: Optional[Callable[[Optional[str]], Optional[str]]] = None):
        self.cfg = config
        # Maps a model id to what ``claude --model`` can invoke (see ``cli_safe_model``). Core
        # cannot import the adapter that owns the real translator, so the config layer passes it.
        self._model_translator = model_translator

    def _launch_settings(self) -> DeepLaunchSettings:
        """The launch parameters in force for the next worker (config first, else the environment-backed core settings)."""
        return resolve_deep_launch(self.cfg.launch)

    def _build_env(self, requester_user_id: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        env = dict(process_environment())
        apply_requester_env(env, requester_user_id)
        env["PYTHONUNBUFFERED"] = "1"
        # The id the brief tells the worker to pass as ``--run`` to a tool call (``core/tools.py``), also in its shell's
        # environment, so a call that forgets the flag still lands on this run's record (its metric result, its tool-call
        # ledger) instead of on none.
        if run_id:
            env["QAR_TOOL_RUN_ID"] = run_id
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

    def _review_liveness(self, working_dir, session_id, elapsed, emit, run_id):
        """Ask a quick LLM whether the run should still be running.

        Returns ``(stop, reason, usage_limit)``. Fails open: any problem reaching or reading the
        reviewer means "keep running", because a wrong stop loses work that was about to land.
        The one-sentence reason is always logged and, when it lets the run continue, emitted to
        the progress feed so it is visible next to the run.
        """
        path = resolve_session_file(working_dir, session_id)
        limit = usage_limit.limit_from_session_file(path) if path else None
        if limit is not None:
            _log.warning("deep run %s hit the Claude usage limit; stopping it to wait", session_id)
            return True, f"it hit the Claude usage limit ({limit.message}).", limit
        idle = -1.0
        try:
            if path is not None:
                idle = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            pass
        # A quiet parent log while a subagent works is real work, never idleness: skip the LLM.
        busy = subagent_work_in_flight(path)
        if busy is not None:
            reason = f"The worker is waiting on a subagent ({busy})."
            _log.info("deep run %s liveness review after %ds: keep running (%s)",
                      session_id, int(elapsed), reason)
            return False, reason, None
        tail = read_session_activity_tail(working_dir, session_id) or "(the session log is empty or unreadable)"
        prompt = DEEP_REVIEW_PROMPT.format(
            elapsed=int(elapsed), idle=("unknown" if idle < 0 else int(idle)), tail=tail)
        verdict = None
        try:
            reply = (self.cfg.reviewer(prompt) if self.cfg.reviewer is not None
                     else self._call_reviewer_cli(prompt))
            verdict = parse_deep_review(reply)
        except Exception as e:  # noqa: BLE001 — a broken reviewer must never kill a healthy run
            _log.warning("deep run liveness review failed (%s); leaving the run alone", e)
        if verdict is None:
            reason = "The liveness review could not be completed, so the run was left alone."
            stop = False
        else:
            stop, reason = verdict
        _log.info("deep run %s liveness review after %ds: %s (%s)", session_id, int(elapsed),
                  "STOP" if stop else "keep running", reason)
        if stop:
            return True, reason, None
        if emit is not None:
            try:
                emit(ProgressEvent(
                    type=EVENT_EXEC,
                    text=f"Not stopping the work: {reason}",
                    data={"run_id": run_id, "phase": "liveness_review", "stopped": False}))
            except Exception:  # noqa: BLE001 — progress is best effort
                pass
        return False, reason, None

    def _call_reviewer_cli(self, prompt: str) -> str:
        """One-shot headless ``claude -p`` on a cheap model; returns the result text."""
        cmd = [self.cfg.claude_path, "-p", "--output-format", "json", "--max-turns", "1"]
        model = cli_safe_model(self.cfg.review_model, self._model_translator)
        if model:
            cmd += ["--model", model]
        import tempfile
        with tempfile.TemporaryDirectory() as neutral:
            done = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True,
                                  cwd=neutral, env=self._build_env(),
                                  timeout=DEEP_REVIEW_CALL_TIMEOUT_SECONDS)
        raw = done.stdout.decode("utf-8", errors="replace") if done.stdout else ""
        out = _parse_worker_output(raw)[0]
        return out or raw

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None,
                 working_dir: Optional[str] = None,
                 resume_session_id: Optional[str] = None,
                 requester_user_id: Optional[str] = None,
                 cancel_check: Optional[Callable[[], bool]] = None) -> DeepResult:
        """Run the goal, then stamp the result with the model id Claude Code resolved it to.

        ``cancel_check`` (optional) is polled every ``CANCEL_POLL_SECONDS`` while the worker runs;
        when it reports True the worker is stopped promptly (interrupt, then terminate after the
        configured grace period, then kill) and the result comes back ``cancelled`` with the
        session id, instead of the worker carrying on until it exits or the liveness review
        notices."""
        landed_already = self.landed_in(working_dir, resume_session_id)
        try:
            res = self.run_goal_once(goal=goal, brief=brief, model=model, max_turns=max_turns,
                                     emit=emit, context_preamble=context_preamble, run_id=run_id,
                                     working_dir=working_dir, resume_session_id=resume_session_id,
                                     requester_user_id=requester_user_id,
                                     cancel_check=cancel_check)
        except Exception as e:  # noqa: BLE001 -- the DeepRunner port never raises (hubofhubs.ports.deep)
            _log.error("deep worker failed unexpectedly: %s: %s", type(e).__name__, e, exc_info=True)
            return DeepResult(met=False, error=f"The deep worker failed unexpectedly: {type(e).__name__}: {e}")
        if res.resolved_model is None and res.session_id:
            res.resolved_model = resolved_model_from_session(
                self.cfg.working_dir if working_dir is None else working_dir, res.session_id)
        self.count_landed(res, working_dir, landed_already)
        return res

    def landed_in(self, working_dir: Optional[str], session_id: Optional[str]) -> Set[str]:
        """The file changes a session already holds, taken before a resumed run so only its own are counted."""
        if not session_id:
            return set()
        return landed_file_changes(self.cfg.working_dir if working_dir is None else working_dir, session_id)

    def count_landed(self, res: DeepResult, working_dir: Optional[str], already: Set[str]) -> None:
        """Put the run's file changes on its result (``DeepResult.changes_landed``), read from its own session record.

        Counts the file-changing tool uses that finished without an error and that were not in the session before this
        run began. Whatever way the run ended, a change it made before it stopped is a change."""
        if res.session_id:
            res.changes_landed = len(landed_file_changes(
                self.cfg.working_dir if working_dir is None else working_dir, res.session_id) - already)

    def run_goal_once(self, *, goal: str, brief: str, model: Optional[str] = None,
                      max_turns: Optional[int] = None,
                      emit: Optional[Callable[[ProgressEvent], None]] = None,
                      context_preamble: Optional[str] = None,
                      run_id: Optional[str] = None,
                      working_dir: Optional[str] = None,
                      resume_session_id: Optional[str] = None,
                      prompt_budget_tokens: Optional[int] = None,
                      requester_user_id: Optional[str] = None,
                      cancel_check: Optional[Callable[[], bool]] = None) -> DeepResult:
        # ``prompt_budget_tokens`` overrides the configured prompt budget for this call only; it is
        # how a launch refused with "Prompt is too long" retries with a tighter fit (see below).
        # ``context_preamble`` is an OPTIONAL PER-CALL override of ``self.cfg.context_preamble``.
        # When the orchestrator forwards a per-task preamble (e.g. an AI rep's pulled persona), it
        # is used for THIS run only; otherwise the runner's configured base preamble applies, so
        # callers that pass nothing see exactly the prior behaviour.
        preamble = self.cfg.context_preamble if context_preamble is None else context_preamble
        # Claude Code is known to be at its usage limit: a worker spawned now can only be refused.
        # Report that as the limit it is, keeping the session to resume, without spawning.
        paused_on = usage_limit.active_limit()
        if paused_on is not None:
            return DeepResult(met=False, error=f"Claude Code usage limit: {paused_on.message}",
                              session_id=resume_session_id, usage_limited=True)
        # THE PROMPT BUDGET. One explicit budget for everything this worker is handed, spent by
        # priority (``core.prompt_budget``): the request is kept, retrieved history is cut first,
        # and the budget can never exceed what this model's window holds.
        from . import prompt_budget
        window_model = cli_safe_model(model, self._model_translator) or model
        budget = prompt_budget.resolve_deep_prompt_budget(
            prompt_budget_tokens or self.cfg.prompt_token_budget, model=window_model)
        prompt, fit = compose_goal_prompt_fitted(goal, brief, preamble=preamble,
                                                 budget_tokens=budget, model=window_model)
        prompt_tokens = prompt_budget.estimate_tokens(prompt)
        if fit.cuts and emit is not None:
            emit(ProgressEvent(
                type=EVENT_EXEC,
                text=("Some background context was left out to fit this run's prompt budget: "
                      + prompt_budget.describe_cuts(fit.cuts) + "."),
                data={"run_id": run_id, "phase": "prompt_budget",
                      "budget_tokens": budget, "tokens_before": fit.tokens_before,
                      "tokens_after": fit.tokens_after,
                      "cuts": [c.describe() for c in fit.cuts]}))
        # RESUME ONLY WHAT FITS. Resuming a session replays its whole transcript ahead of this
        # prompt, and a thread resumed pass after pass grows without bound until the model refuses
        # it outright ("Prompt is too long", 2026-10-06: a 5 MB transcript holding every earlier
        # pass). When the transcript plus this prompt would not fit the window, the run starts
        # fresh: its brief already carries the compact carry-over (the last results), so a cold
        # start loses nothing a resume could still have given it.
        if resume_session_id:
            replay = session_replay_tokens(
                self.cfg.working_dir if working_dir is None else working_dir, resume_session_id)
            room = prompt_budget.max_prompt_tokens_for(window_model)
            if replay is not None and replay + prompt_tokens > room:
                _log.info("not resuming session %s: its transcript (~%d tokens) plus this prompt "
                          "(~%d tokens) would exceed the ~%d tokens the model can take; starting "
                          "fresh", resume_session_id, replay, prompt_tokens, room)
                if emit is not None:
                    emit(ProgressEvent(
                        type=EVENT_EXEC,
                        text=("The earlier session is too long to pick up again, so this run "
                              "starts fresh from its brief."),
                        data={"run_id": run_id, "phase": "resume_too_large",
                              "replay_tokens": replay, "prompt_tokens": prompt_tokens}))
                resume_session_id = None
        # ``working_dir`` is an OPTIONAL PER-CALL override of ``self.cfg.working_dir`` (e.g. a
        # quest's synced folder, see quest_autopilot_design.md's execution-environment section).
        # Used for THIS run's subprocess cwd AND the session monitor's search root; the consumer's
        # configured global working_dir is untouched (no shared-state mutation, so concurrent
        # tasks with different folders never race on it). Falls back to the configured default
        # exactly as before when omitted.
        effective_working_dir = self.cfg.working_dir if working_dir is None else working_dir
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
        # The argv is built once, below, by ``deep_launch.build_deep_argv`` (a pure function shared with the streaming path):
        # explicit tool gating (``allowed_tools`` / ``disallowed_tools``, kept in lock-step with web_enabled() so what the env
        # ADVERTISES matches what the worker is allowed), ``--mcp-config``, and the launch parameters (``self._launch_settings``).
        # The worker is Claude Code, which ONLY runs Claude models. The orchestrator resolves the
        # model from the consumer's tier config, which in a Gemini/OpenAI deployment is a NON-Claude
        # id (e.g. "gemini-3.5-flash"). Passing that as --model makes Claude Code error ("issue with
        # the selected model ... it may not exist or you may not have access") and the deep run does
        # nothing. So pass --model ONLY when it is a Claude model; otherwise omit it and let the
        # worker use its own configured default.
        # ...and it must be a model the CLI can actually INVOKE, not merely one that looks like a
        # Claude id: a family-bucket label ("claude-sonnet") passes the syntactic check but makes
        # the binary error out on every attempt. cli_safe_model() translates (label -> alias) and
        # returns None for anything the worker can't run. Those runs take the balanced tier
        # (BALANCED_CLI_MODEL) rather than the worker's own default, which can be Opus.
        runnable = cli_safe_model(model, self._model_translator)
        if model and not runnable:
            _log.debug("deep worker is Claude Code; model %r is not runnable, using balanced %r",
                       model, BALANCED_CLI_MODEL)
        turns = max_turns if max_turns is not None else DEFAULT_DEEP_MAX_TURNS

        # Bind this run to an explicit, deterministic session id (rather than letting Claude Code
        # pick one) so the progress monitor can watch EXACTLY this run's session file instead of
        # guessing from "any new jsonl" — a guess that can cross-attach to a concurrent session
        # (the parent conversation, or another deep run sharing the same project dir).
        #
        # CONTINUATION: ``resume_session_id`` is a session THIS goal already ran and did not get to
        # finish (it ran out of turn budget). Resuming it means the worker continues with everything
        # it already read and decided, instead of paying for that discovery a second time and very
        # likely running out of room again at the same place. The id stays the same across the whole
        # continuation, so the monitor and the transcript stay one thread of work.
        if resume_session_id:
            session_id, session_flag = resume_session_id, "--resume"
        else:
            session_id, session_flag = str(uuid.uuid4()), "--session-id"
        cmd = build_deep_argv(
            binary, streaming=False, skip_permissions=self.cfg.skip_permissions,
            allowed_tools=self.cfg.allowed_tools, disallowed_tools=self.cfg.disallowed_tools,
            mcp_config_path=self.cfg.mcp_config_path, model=runnable or BALANCED_CLI_MODEL, turns=turns,
            goal_given=bool(goal.strip()), session_flag=session_flag, session_id=session_id,
            launch=self._launch_settings())

        # No fixed kill by default: an optional hard cap (config or QAR_DEEP_TIMEOUT_SECONDS) and a
        # periodic LLM liveness review of the session log decide when a run must stop.
        effective_timeout = resolve_deep_timeout_seconds(self.cfg.timeout_seconds)
        review_interval = resolve_deep_review_interval_seconds(self.cfg.review_interval_seconds)

        # Start monitoring Claude Code session in a background thread if emit is provided.
        stop_monitor = threading.Event()
        monitor_thread = None
        cutoff_time = time.time()  # Sessions created after this are new
        if emit is not None:
            _log.info("starting claude session monitor for deep run in: %s (session_id=%s)",
                     effective_working_dir, session_id)
            monitor_thread = threading.Thread(
                target=_monitor_claude_session,
                args=(effective_working_dir, emit, stop_monitor, cutoff_time),
                kwargs={"forced_run_id": run_id, "session_id": session_id},
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
                cwd=effective_working_dir,
                env=self._build_env(requester_user_id, run_id),
                # Own process group: on a timeout we must kill the worker AND every child it
                # spawned, not just the top pid. See kill_process_group().
                start_new_session=True,
            )
        except FileNotFoundError:
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=1)
            return DeepResult(met=False, launch_failed=True, launch_problem=LAUNCH_MISSING_PROGRAM,
                              error=f"The deep worker could not start: binary not found "
                                    f"({self.cfg.claude_path}).")
        except PermissionError as e:
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=1)
            return DeepResult(met=False, launch_failed=True, launch_problem=LAUNCH_PERMISSION_DENIED,
                              error=f"The deep worker could not start: permission denied ({e}).")

        # Communicate with the process (send prompt and wait for completion) in slices of one review
        # interval. At each slice end a quick LLM reads the live session log and stops the run ONLY
        # if it should no longer be running; otherwise the one-sentence reason it was left alone
        # is logged (and shown in the progress feed) and the wait continues.
        proc_start = time.time()
        stop_reason: Optional[str] = None
        stop_limit = None
        stopped_how: Optional[str] = None   # set when a cancel request stopped the worker
        pending_input: Optional[bytes] = prompt.encode("utf-8")
        # With a cancel signal the wait is cut into short slices so a Stop is noticed within
        # ``CANCEL_POLL_SECONDS``; the liveness review then runs on its own clock inside them.
        # Without one the slices are the review interval, exactly as before.
        poll_s = CANCEL_POLL_SECONDS if cancel_check is not None else None
        next_review = (proc_start + review_interval) if review_interval is not None else None
        try:
            while True:
                now = time.time()
                slice_s = review_interval
                if poll_s is not None and next_review is not None:
                    slice_s = min(poll_s, max(0.0, next_review - now))
                elif poll_s is not None:
                    slice_s = poll_s
                if effective_timeout is not None:
                    left = max(0.0, effective_timeout - (now - proc_start))
                    slice_s = left if slice_s is None else min(slice_s, left)
                try:
                    raw, err = proc.communicate(input=pending_input, timeout=slice_s)
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None  # the prompt is only ever sent on the first call
                    if cancel_check is not None:
                        try:
                            cancel_now = bool(cancel_check())
                        except Exception:  # noqa: BLE001 -- a broken check must never end a run
                            cancel_now = False
                        if cancel_now:
                            raw, err, stopped_how = stop_process_group(
                                proc, self.cfg.cancel_grace_seconds)
                            break
                    elapsed = time.time() - proc_start
                    if effective_timeout is not None and elapsed >= effective_timeout:
                        stop_reason = (
                            f"Deep run exceeded its configured wall-clock timeout: ran for "
                            f"{elapsed:.0f}s against a {effective_timeout:.0f}s limit.")
                        break
                    if review_interval is None:
                        continue
                    if poll_s is not None:
                        if time.time() < next_review:
                            continue
                        next_review = time.time() + review_interval
                    stop_now, reason, stop_limit = self._review_liveness(
                        effective_working_dir, session_id, elapsed, emit, run_id)
                    if stop_now:
                        stop_reason = reason
                        break
            if stop_reason is not None:
                kill_process_group(proc)
                # Drain whatever the worker had already buffered, purely for diagnostics.
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001 — best-effort drain after a kill, never raise here
                    pass
        finally:
            # Stop monitoring thread
            stop_monitor.set()
            if monitor_thread:
                monitor_thread.join(timeout=2)

        if stopped_how is not None:
            # STOPPED ON REQUEST. The worker process group was interrupted (and terminated or killed
            # when it ignored that); whatever it had printed is kept, and the session id lets a
            # later reply resume the work already done. Not a failure and never verified.
            partial_raw = raw.decode("utf-8", errors="replace") if raw else ""
            partial, stop_tokens, stop_cost = _parse_worker_output(partial_raw)[:3]
            _log.info("deep run %s stopped on request (%s)", session_id, stopped_how)
            return DeepResult(
                met=False, output=partial, tokens=stop_tokens, cost_usd=stop_cost,
                session_id=session_id, cancelled=True,
                error="The run was stopped on request. Changes it had already finished are kept; "
                      "a step it was in the middle of may not have completed.")
        if stop_reason is not None:
            if stop_limit is not None:
                stop_limit = usage_limit.record(stop_limit)
                return DeepResult(met=False, output="", session_id=session_id, usage_limited=True,
                                  error=f"Claude Code usage limit: {stop_limit.message}")
            activity = describe_session_activity(effective_working_dir, session_id)
            return DeepResult(
                met=False,
                session_id=session_id,
                error=(
                    f"Stopped by the liveness review: {stop_reason} The worker process group was "
                    "killed. This is a hard failure, not a silent success, even if partial output "
                    "exists." + (f"\n\n{activity}" if activity else "")
                ),
            )

        raw = raw.decode("utf-8", errors="replace") if raw else ""
        err = err.decode("utf-8", errors="replace") if err else None
        # We requested ``--output-format json``: parse the worker's final result text AND its
        # reported token usage / cost out of the JSON envelope. ``out`` becomes the human-readable
        # result; ``tokens``/``cost`` feed the goal loop's overall token budget. If parsing fails
        # (older worker, plain text), fall back to treating stdout as the result with no usage.
        out, tokens, cost, json_is_error, subtype = _parse_worker_output(raw)

        # RESUME IS BEST EFFORT. The session we were asked to continue is gone (pruned, another
        # machine, a different project directory), so the worker refused before doing anything at
        # all. Continuity is an optimisation; the WORK is the point. Run the goal once more from a
        # cold start, which is precisely what this run would have been had no session been offered.
        # One retry only, because the retry passes ``resume_session_id=None`` and so can never come
        # back here. Any other resume failure is left alone and reported as the failure it is.
        if resume_session_id and resume_target_missing(proc.returncode, out, err):
            _log.info("resume target %s is no longer available; running this goal from a cold "
                      "start instead", resume_session_id)
            if emit is not None:
                emit(ProgressEvent(
                    type=EVENT_EXEC,
                    text="The earlier session is no longer available, so this run starts fresh.",
                    data={"run_id": run_id, "phase": "resume_unavailable"}))
            return self.run_goal(goal=goal, brief=brief, model=model, max_turns=max_turns,
                                 emit=emit, context_preamble=context_preamble, run_id=run_id,
                                 working_dir=working_dir, resume_session_id=None,
                                 cancel_check=cancel_check)

        # THE SUBSCRIPTION USAGE LIMIT is a wait, not a failure. Claude Code refuses with exit 1 and
        # a synthetic result ("You've hit your weekly limit · resets 1pm (America/Los_Angeles)");
        # its session record says the same structurally (quotaLimits.resetsAt). Checked only on an
        # error exit, so ordinary output that merely mentions limits is never mistaken for one.
        # The session id travels with it: the work done before the refusal lives in that session,
        # and the resumed run continues it.
        if proc.returncode != 0 or json_is_error:
            limit = (usage_limit.limit_from_session_file(
                        resolve_session_file(effective_working_dir, session_id))
                     or usage_limit.detect_usage_limit(out)
                     or usage_limit.detect_usage_limit(err or ""))
            if limit is not None:
                limit = usage_limit.record(limit)
                return DeepResult(met=False, output="", tokens=tokens, cost_usd=cost,
                                  session_id=session_id, usage_limited=True,
                                  error=f"Claude Code usage limit: {limit.message}")

        # The escalation-marker contract: the worker raised a human decision mid-run and printed
        # ``QAR-ESCALATED: <decision_id>``. That overrides met-vs-limit — the run is PAUSED on a
        # human, so the executor must report needs_you with the decision linked, regardless of
        # the exit code.
        decision_id = extract_escalation_id(out)
        if decision_id:
            return DeepResult(met=False, output=out, decision_id=decision_id,
                              tokens=tokens, cost_usd=cost, session_id=session_id)
        if proc.returncode == 0 and not json_is_error:
            # Safety net against a SILENT NO-OP: a real headless ``-p`` run always prints its final
            # result, so exit-0 with EMPTY output means the worker never actually ran the goal (the
            # failure mode when ``-p`` is missing, or the binary mis-launches). Report it as a
            # failure with a clear message instead of a hollow "met" that shows "Completed" but did
            # nothing. (A pure chit-chat run has an empty ``goal`` and is exempt.)
            if goal.strip() and not out.strip():
                # Before settling for that diagnosis, check the run's OWN session record: if it
                # shows real work, "never actually ran the goal" would be a false assertion, so
                # report what the record shows instead and leave -p as one named possibility.
                activity = describe_session_activity(effective_working_dir, session_id,
                                                     worker_output=out)
                if activity:
                    return DeepResult(
                        met=False, output=out, tokens=tokens, cost_usd=cost, session_id=session_id,
                        error="worker exited cleanly but produced NO output, so the goal cannot be "
                              "confirmed to have run (one common cause: the worker did not run "
                              "headless, e.g. Claude Code needs -p).\n\n" + activity)
                return DeepResult(
                    met=False, output=out, tokens=tokens, cost_usd=cost, session_id=session_id,
                    error="worker exited cleanly but produced NO output, so the goal did not "
                          "actually run (check that the worker runs headless, e.g. Claude Code "
                          "needs -p).")
            return DeepResult(met=True, output=out, tokens=tokens, cost_usd=cost,
                              session_id=session_id)
        # THE WORKER NEVER STARTED. Claude Code refused before its first model turn (an
        # unrecognized ``--model``, bad credentials, a rejected flag): it exits non-zero with an
        # error envelope that reports ZERO tokens used, and its ``result`` is the CLI's own error
        # message ("There's an issue with the selected model ..."). That text is not work product.
        # Handed on as ordinary output, the goal loop verified it, called it "goal not met" and
        # retried on a stronger model, all within a couple of seconds, which reads to a person as
        # a deep run that ran and fell short. It is a launch failure, and it says so.
        if envelope_reports_no_work(raw):
            reason = (out or "").strip() or (err or "").strip() or f"exit {proc.returncode}"
            if prompt_budget.is_prompt_too_long(reason):
                # The model refused the prompt as too long for its window. Never launch the same
                # thing again: drop the resumed transcript first (the usual culprit), then halve
                # the budget, and only report a failure when neither fits.
                if resume_session_id:
                    _log.info("prompt too long while resuming %s; running fresh instead",
                              resume_session_id)
                    return self.run_goal_once(
                        goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit,
                        context_preamble=context_preamble, run_id=run_id,
                        working_dir=working_dir, resume_session_id=None,
                        prompt_budget_tokens=budget, requester_user_id=requester_user_id,
                        cancel_check=cancel_check)
                tighter = budget // 2
                if tighter >= prompt_budget.MIN_DEEP_PROMPT_TOKEN_BUDGET:
                    _log.info("prompt too long at a %d-token budget (~%d tokens sent); retrying "
                              "at %d", budget, prompt_tokens, tighter)
                    return self.run_goal_once(
                        goal=goal, brief=brief, model=model, max_turns=max_turns, emit=emit,
                        context_preamble=context_preamble, run_id=run_id,
                        working_dir=working_dir, resume_session_id=None,
                        prompt_budget_tokens=tighter, requester_user_id=requester_user_id,
                        cancel_check=cancel_check)
                return DeepResult(
                    met=False, output="", session_id=session_id, launch_failed=True,
                    error=(f"The deep worker could not start: the model refused the prompt as too "
                           f"long for its context window, even after it was cut down to about "
                           f"{prompt_tokens} tokens. Nothing was done in this run. ({reason[:200]})"))
            return DeepResult(
                met=False, output="", session_id=session_id, launch_failed=True,
                error=f"The deep worker could not start: {reason[:600]}")
        if not err:
            # No stderr to quote. Say exactly that rather than ASSERTING a cause: the old wording
            # ("likely hit the turn/budget limit") was a guess printed as fact, and it is the text
            # a human reads on a failed task, so it sent every diagnosis down the wrong path. The
            # worker's own output IS carried on this result, so point the reader at it.
            tail = (out or "").strip()
            if subtype == "error_max_turns":
                # The envelope SAYS how it ended, so say that instead of listing "common causes".
                # This is not a crash: the worker ran out of room mid-flight, so its work may be
                # complete, partially done, or barely started, and only the output below tells you
                # which. (Live case: a daily brief whose last acts were sending the mail and
                # writing its goal note, reported as a bare failure because the turn budget ended
                # one beat later. Raise the lane's ``deep_max_turns`` if that keeps happening.)
                err = (f"The worker used its entire {int(turns)}-turn budget without declaring the "
                       "goal met, so the goal is UNCONFIRMED rather than failed. Whatever it "
                       "finished before the budget ran out still happened; read the run output "
                       "below to see how far it got.")
            else:
                err = (f"The worker exited {proc.returncode} with no error output. The goal was not "
                       "confirmed met. Common causes: the turn or token budget ran out, or the "
                       "worker itself errored. Read the run output below for what it actually did.")
            if tail:
                err = f"{err}\n\nLast output:\n{tail[-1500:]}"
            # A worker killed/crashed BEFORE printing its final JSON envelope leaves ``tail`` empty
            # (or trivially short), so the message above would carry nothing about a run that may
            # have read, written and executed plenty. Its session record did capture all of that —
            # surface the tail of it. Absent/unreadable record: the bare message stands, as today.
            if worker_output_is_thin(tail):
                activity = describe_session_activity(effective_working_dir, session_id,
                                                     worker_output=tail)
                if activity:
                    err = f"{err}\n\n{activity}"
        # ``limit_hit`` is the worker's OWN statement that it ran out of turns (not our guess), so
        # the goal loop can continue this session instead of starting the goal over.
        return DeepResult(met=False, output=out, error=err, tokens=tokens, cost_usd=cost,
                          session_id=session_id, limit_hit=(subtype == "error_max_turns"))
