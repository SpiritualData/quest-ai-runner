"""One chat turn: start, stream, render each progress event, finish, queue draining and the footer.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence
from rich.text import Text
from textual import work

from .interactive_session import _DeepRunTracker, _model_label
from .interactive_session_tracker import format_elapsed
from .textual_ui_widgets import (
    _AWAITING_DECISION_PLACEHOLDER,
    _READY_PLACEHOLDER,
    _busy_placeholder,
)
from .textual_ui_prompt import PromptTextArea
from .core.metric_results import without_line
from .textual_ui_helpers import _build_shallow_context_bullets, context_block_lines, plural
from .textual_ui_outcome import (
    MAX_AUTO_PASSES,
    OUTCOME_FINISHED,
    OUTCOME_LIMIT,
    allows_second_pass,
    closing_text,
    launch_failure_lines,
    limit_reset,
    model_text,
    provider_error_lines,
    speaker_label,
    turn_outcome,
)


class TurnMixin:
    """One chat turn: start, stream, render each progress event, finish, queue draining and the footer."""

    def _types(self) -> dict:
        if self._ev is None:
            from .core.adapters import (
                EVENT_CONTEXT, EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN,
                EVENT_PARTIAL, EVENT_EXEC, EVENT_INTENT, EVENT_RESULT, EVENT_DECISION,
                EVENT_MILESTONE, EVENT_DONE, EVENT_UNDERSTANDING,
            )
            self._ev = dict(
                context=EVENT_CONTEXT, status=EVENT_STATUS, plan=EVENT_PLAN,
                read=EVENT_READ, replan=EVENT_REPLAN, partial=EVENT_PARTIAL,
                exec=EVENT_EXEC, intent=EVENT_INTENT, result=EVENT_RESULT,
                decision=EVENT_DECISION,
                milestone=EVENT_MILESTONE, done=EVENT_DONE,
                understanding=EVENT_UNDERSTANDING,
            )
        return self._ev

    def _begin_turn(self, user_text: str, *, echo: bool, auto: bool = False,
                    resume_session_id: Optional[str] = None,
                    request: Optional[str] = None,
                    deep_goal: Optional[str] = None) -> None:
        if self._turn_active:
            return
        self._turn_active = True
        self._cancel.clear()
        # This turn's control channel (steer, stop, pause; textual_ui_control.py). It only does
        # anything for a deep worker that holds a live session; every other worker never sees it.
        self._run_control = self._new_terminal_control()
        self._turn_request = request if request is not None else user_text
        self._resume_session_id = resume_session_id
        # A turn that goes straight to a deep run of this goal, with no planner routing (/resume).
        self._deep_goal = deep_goal
        self._partial_started = False
        self._ai_label_shown = False
        self._cur_deep_run = None
        self._answer_parts: List[str] = []
        self._t0 = time.monotonic()
        self._deep = _DeepRunTracker()
        self._deep_seen = set()
        self._deep_event_count = 0
        self._deep_flushed = set()
        self.metrics_shown.clear()
        self._future_context = ""
        self._last_narration = ""
        self.pending_question = ""
        self.retry_text = user_text if echo else ""
        self._ctx.reset()
        self._deep_view.hide()
        self._deep_detail.hide()
        self._future_ctx_panel.hide()

        if auto:
            self._auto_pass += 1
        else:
            self._auto_pass = 1

        self._set_terminal_title("Quest AI Runner")

        if echo:
            self._tlog.write(Text(f"❯ {user_text}", style="bold cyan"))
            self._tlog.write(Text(""))

        # Loading strip on; keep input enabled so mid-turn messages can be queued. Reset the
        # queued-count indicator: any messages left over from a previous turn were already
        # folded into THIS turn's starting text (see _finish_turn), so none are outstanding.
        self._activity.set_status("Thinking…")
        self._activity.start_clock()
        self._activity.set_queued(0)
        self._activity.display = True
        self.query_one("#prompt", PromptTextArea).placeholder = _busy_placeholder(0)

        self._run_stream(user_text)

    @work(thread=True, exclusive=True)
    def _run_stream(self, user_text: str) -> None:
        """Iterate the orchestrator stream in a worker thread."""
        from .core.orchestrator import OrchestratorResult

        s = self.sess
        model_hint = s._model_hint
        if s._replan_next:
            model_hint = "quality"
            s._replan_next = False
            self.call_from_thread(self._console.dim, "  Replan mode: using the quality tier for this turn.")

        final: Optional["OrchestratorResult"] = None
        error: Optional[Exception] = None
        try:
            _inbox = getattr(s._orch, "input_inbox", None)
            _sid = self._session_id
            _pending = self._make_pending_drain(_inbox, _sid) if _inbox is not None else None
            preamble, grounding_meta = s.turn_grounding(user_text)
            if s.turn_quest is not None:
                from .runner.quest_folder_index import describe_match
                how = "Selected" if s.turn_quest is s.pinned_quest else "Matched"
                self.call_from_thread(self._console.dim,
                                      f"  {how} {describe_match(s.turn_quest)}")
            stream = s._orch.run_stream(
                user_text,
                transcript=s._last_transcript(),
                quest_id=s.bound_quest_id(),
                rep_preamble=preamble,
                model_hint=model_hint,
                pending_inputs=_pending,
                conv_id=s._conv_id,
                context_meta=grounding_meta,
                cancel_check=self._cancel.is_set,
                **self._control_stream_kwargs(),
            )
            try:
                for item in stream:
                    if self._cancel.is_set():
                        control = self._run_control
                        if control is None or not control.in_use:
                            break
                        # A live worker is being stopped: let it finish stopping so its result
                        # (the session a /resume continues, any message it never confirmed)
                        # reaches _finish_turn. Nothing more is rendered meanwhile.
                        if isinstance(item, OrchestratorResult):
                            final = item
                        continue
                    if isinstance(item, OrchestratorResult):
                        final = item
                    else:
                        self.call_from_thread(self._handle_event, item)
            finally:
                # Esc: closing the stream cancels the run (a deep worker process is interrupted)
                # and waits for it to stop, so the leftover-message turn that _finish_turn may
                # start can never run beside the one that was cancelled.
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
        except Exception as e:  # noqa: BLE001
            error = e

        elapsed = time.monotonic() - self._t0
        self.call_from_thread(self._finish_turn, user_text, final, elapsed,
                              self._cancel.is_set(), error)

    def _control_stream_kwargs(self) -> dict:
        """The extra ``run_stream`` arguments for steer, stop, pause and resume, only when set.

        ``deep_goal`` is what makes ``/resume`` deterministic: the turn goes straight to a deep run
        of the paused goal in the paused session, whatever the planner would have chosen.
        """
        extra: dict = {}
        if self._run_control is not None:
            extra["run_control"] = self._run_control.run_control()
        if getattr(self, "_resume_session_id", None):
            extra["resume_session_id"] = self._resume_session_id
        if getattr(self, "_deep_goal", None):
            extra["deep_goal"] = self._deep_goal
        return extra

    def _handle_event(self, event) -> None:
        """Render one ProgressEvent (runs on the UI thread)."""
        if isinstance(event, dict):
            t = event.get("type", "")
            text = (event.get("text") or "").rstrip()
            action = event.get("action") or ""
            data = event.get("data") or {}
            result_kind = event.get("result_kind") or ""
        else:
            t = event.type
            text = (event.text or "").rstrip()
            action = getattr(event, "action", None) or ""
            data = event.data or {}
            result_kind = getattr(event, "result_kind", None) or ""
        ev = self._types()
        log = self._tlog

        if t == ev["partial"]:
            # Narration beats (the instant ack + the planner's conversational rationale) come as
            # EVENT_PARTIAL tagged data={"narration": True} (legacy: "ack"). Write each beat into
            # the main transcript feed, inline above where the answer will land — the reasoning of
            # what's happening reads as part of the conversation, not a separate strip at the
            # bottom. Beats accumulate one per line as the AI progresses; skip exact consecutive
            # repeats so a re-emitted beat doesn't double up.
            is_ack = isinstance(data, dict) and (data.get("narration") or data.get("ack"))
            if is_ack:
                beat = text.strip()
                if beat and beat != self._last_narration:
                    self._last_narration = beat
                    log.write(Text(f"  {beat}", style="italic dim"))
                return
            # Accumulate streamed answer; render once at the end (calm display).
            self._partial_started = True
            self._answer_parts.append(text)
            self._activity.set_status("Answering…")
            return

        if t == ev["status"]:
            self._activity.set_status(text or "Thinking…")

        elif t == ev["plan"]:
            if action == "deep":
                # Don't echo the internal routing rationale — just signal the mode.
                log.write(Text("  ▸ Deep execution", style="dim"))
                # The run is being prepared, not started: the status line says so (with the
                # turn's clock, since this can take minutes) until the core reports a real phase
                # as a status event, and the first exec event hands over to the deep dashboard.
                self._activity.set_status("Preparing the deep run…")
            else:
                if text:
                    label = f"▸ {action}" if action else "▸ plan"
                    log.write(Text(f"  {label}  {text}", style="dim"))
                self._activity.set_status("Planning…")

        elif t == ev["replan"]:
            self._ctx.inc_replans()
            if text:
                log.write(Text(f"  ↺ replan  {text}", style="dim"))
            self._activity.set_status("Re-planning…")

        elif t == ev["read"]:
            paths = data.get("sources") or []
            self._ctx.add_sources(paths)
            total = self._ctx.total_sources
            self._activity.set_status(
                f"Gathering context  ({total} source{'s' if total != 1 else ''} so far)"
            )

        elif t == ev["context"]:
            card_meta = data.get("card_metadata") or []
            sources = data.get("sources") or []
            if card_meta:
                # The title stays what the app is called: the best-scoring card is a guess at what
                # the message is about, not the quest the person is working on.
                self._ctx.set_cards(card_meta)
            # One calm block; the per-card detail (files, scores) is in the live context panel.
            for line in context_block_lines(card_meta, sources, self.transcript_width()):
                log.write(line)

        elif t == ev["understanding"]:
            # Stage 1's resolved goal condition, surfaced the instant it's ready (well before
            # any plan/answer). Informational only, not blocking, so it gets its own distinct
            # look (cyan diamond) instead of the yellow decision marker below.
            if text:
                log.write(Text(""))
                log.write(Text(f"  ◆ {text}", style="bold cyan"))

        elif t == ev["exec"]:
            run_id = data.get("run_id") or "default"
            goal = (data.get("goal") or "").strip()
            goal = goal[:1].upper() + goal[1:] if goal else ""
            first_event_of_run = run_id not in self._deep_seen
            if first_event_of_run:
                self._deep_seen.add(run_id)
                self._deep.add_run(run_id, goal or "Executing work…")
            elif goal:
                # A later event may carry the real subgoal even if the first didn't; keep it current.
                self._deep.update_goal(run_id, goal)
            self._cur_deep_run = run_id
            if text:
                # A heartbeat (the worker's sign of life, named by its phase) shows live but is not
                # one of the run's actions, so it stays out of the record and the Alt+D replay.
                is_heartbeat = isinstance(data, dict) and data.get("phase") == "heartbeat"
                self._deep.update_run_output(run_id, text, record=not is_heartbeat)
                # Push to detail panel live (it scrolls / holds full history).
                self._deep_detail.push_line(run_id, text)
            # Show a run's dashboard the moment its first event lands (a run with fewer than ten
            # events used to show nothing live), then throttle redraws to avoid flicker.
            self._deep_event_count += 1
            if first_event_of_run or self._deep_event_count % 10 == 0:
                self._refresh_deep_dashboard()
            self._activity.set_status("Executing…")
            self._activity.stop_clock()   # the deep dashboard shows the run's progress from here
            # A terminal phase means this deep task is finished: persist its full
            # output to the scrollback transcript now, so it stays readable after
            # the live deep widgets are gone. Each task leaves a permanent record.
            from .core.guard import classify_exec_phase
            outcome = classify_exec_phase(data.get("phase") if isinstance(data, dict) else None)
            if outcome is not None:
                self._deep.set_run_status(run_id, "done" if outcome == "success" else "error")
                self._flush_deep_run(run_id)

        elif t == ev["milestone"]:
            # A completed deep subtask carries its full output; stash it on the run so the
            # scrollback record can show what the task actually produced (the result), not just
            # the trace of steps it took.
            if isinstance(data, dict):
                rid = data.get("run_id")
                deep_out = data.get("deep_output")
                metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
                metric_missing = data.get("metric_missing") is True
                if rid and deep_out:
                    # The library ends a result's text with one plain "Metric: ..." line for consumers
                    # that show text only. This terminal writes the entries as lines of its own, so that
                    # line is left out of the result and the numbers appear once.
                    self._deep.set_final_output(rid, without_line(deep_out, metrics, metric_missing))
                if rid and (metrics or metric_missing):
                    self._deep.set_metrics(rid, metrics, metric_missing)
                    if rid in self._deep_flushed:
                        # The run's record was written before its milestone arrived: the lines follow it.
                        self.write_metrics(metrics, metric_missing)
            # The milestone's own text ("Completed: <goal>", a not-met attempt's output, a clarifying
            # question) is not echoed: it printed a green tick before the work was shown, and for a
            # goal that was not met it printed the tick anyway. The run's record, the answer and the
            # turn's closing line say what happened, in the order it did.

        elif t == ev["intent"]:
            # "Executing: <goal>" — an announcement that work is about to start. Written straight
            # to the transcript as a progress line and DELIBERATELY kept out of _answer_parts:
            # _finish_turn falls back to _answer_parts when a deep turn flushed nothing, so an
            # interim announcement landing in there came back out as the turn's final answer,
            # reading as a completion report for work that never ran.
            if text:
                log.write(Text(""))
                log.write(f"  [cyan]▸[/cyan]  {text}")

        elif t == ev["result"]:
            # Non-streamed answers arrive here; streamed ones are in _answer_parts.
            if not self._partial_started and text:
                self._answer_parts.append(text)
            # Capture future context from deep result events.
            if result_kind == "deep" and isinstance(data, dict):
                fc = (data.get("future_context") or "").strip()
                if fc:
                    self._future_context = fc

        elif t == ev["decision"]:
            # The question is written once, as the last thing of the turn (``write_pending_question``):
            # written now it would be followed by the rest of the run's record and a closing line. Until
            # then the prompt keeps taking messages for the running turn, so its placeholder is not
            # changed to point at a question that is not on screen yet.
            self._awaiting_decision = True
            self.pending_question = text
            self._activity.set_status("Waiting for your answer…")

        # done: terminal signal only.

    def transcript_width(self) -> int:
        """Columns a transcript line can use before it would wrap (80 when the size is unknown)."""
        width = 0
        try:
            width = self._tlog.scrollable_content_region.width
        except Exception:  # noqa: BLE001
            pass
        if not width:
            try:
                width = self.size.width - 4
            except Exception:  # noqa: BLE001
                pass
        return width - 1 if width >= 21 else 80

    def _make_pending_drain(self, inbox: "InputInbox", sid: str) -> Callable[[], List[str]]:
        """Wrap the orchestrator's own ``pending_inputs`` drain so a mid-run pickup is VISIBLE.

        ``inbox.drain`` is called from the worker thread by the orchestrator's own retry/improve
        loops (a long deep run folding steering into its next attempt). Calling it directly left
        the "↑ queued (N): ..." line written at queue-time sitting on screen forever with no sign
        it was ever acted on — the message was in fact picked up, but only the orchestrator's own
        loop ever knew that. Wrapping it reports the pickup back to the UI thread the same way
        ``_finish_turn``'s end-of-turn drain already does.
        """
        def _drain() -> List[str]:
            msgs = inbox.drain(sid)
            if msgs:
                remaining = len(inbox.peek(sid))
                self.call_from_thread(self._note_queued_folded_in, msgs, remaining)
            return msgs
        return _drain

    def _note_queued_folded_in(self, messages: List[str], remaining: int) -> None:
        """UI-thread callback: the running turn just picked up queued message(s) mid-run."""
        n = len(messages)
        self._tlog.write(Text(
            f"  ↓ {n} queued message{'s' if n != 1 else ''} picked up mid-run",
            style="dim yellow",
        ))
        self._activity.set_queued(remaining)
        self.query_one("#prompt", PromptTextArea).placeholder = _busy_placeholder(remaining)

    def _drain_queued_inputs(self) -> List[str]:
        """Pull (and clear) whatever this session's messages are still sitting in the inbox.

        Called once a turn is fully done (normal finish OR an Escape cancel). Anything the
        orchestrator's own goal loop already drained mid-run (a long deep run folding steering
        into its next attempt) is gone from the inbox by then, so this only ever returns messages
        that turn never got a chance to act on -- the case that used to leave a message marked
        "queued" in the transcript forever with nothing done about it.
        """
        if self.sess is None:
            return []
        inbox = getattr(self.sess._orch, "input_inbox", None)
        if inbox is None:
            return []
        return [m for m in inbox.drain(self._session_id) if m and m.strip()]

    def _finish_turn(self, user_text: str, final, elapsed: float,
                     cancelled: bool, error: Optional[Exception]) -> None:
        """Wrap up a turn on the UI thread: answer, footer, bookkeeping."""
        s = self.sess
        log = self._tlog
        self._activity.display = False
        self._activity.stop_clock()
        self._ctx.display = False
        # Persist any deep task output that didn't already emit a terminal phase
        # (incl. cancelled/errored turns) before the live widgets disappear.
        self._flush_pending_deep_runs()
        self._deep_view.hide()
        self._deep_detail.hide()
        # Steer, stop, pause: put back what a live worker never confirmed, hold what a paused run
        # keeps for /resume, remember the session a /resume continues.
        ended = self._settle_run_control(final, cancelled, error)

        if error is not None:
            self.write_provider_error(error)
        elif cancelled or ended == "paused":
            paused = ended == "paused"
            self._console.dim("  Paused." if paused else "  Cancelled.")
            if ended:
                self._console.dim("  Finished work is kept. /resume continues the same session.")
            marker = "[paused by user]" if paused else "[cancelled by user]"
            s._last_user = user_text
            s._last_assistant = marker
            s._session_history.append((user_text, marker))
            s._write_session_file()
            ctx = getattr(s._orch, "context_assembler", None)
            if ctx is not None:
                try:
                    ctx.record(user_text, {"kind": "cancelled",
                                           "response": "[turn was paused by user]" if paused
                                           else "[turn was cancelled by user]"})
                except Exception:  # noqa: BLE001
                    pass
        elif final is not None:
            self._record_turn(final, elapsed)
            self._maybe_handle_deep_plan(final)   # prints planned changes (once)
            # A deep turn's output already got a full per-run record (header, activity summary,
            # result markdown) written to scrollback via _flush_pending_deep_runs() above, driven
            # from the SAME text the orchestrator's terminal EVENT_RESULT carries for a "deep" kind
            # result. _answer_parts (the fallback used below) picks that EVENT_RESULT text back up
            # since deep runs never stream via EVENT_PARTIAL, so printing it again here duplicated
            # the entire result body under a second "{rep} (AI): Executing: ..." bubble (2026-07-26
            # bug report). Only fall back to the generic answer bubble when nothing was actually
            # flushed to scrollback this turn (e.g. a deep run that captured no output at all).
            #
            # The _answer_parts fallback is REAL result text only. The "Executing: <goal>"
            # announcement no longer arrives as a result event (it is EVENT_INTENT now and never
            # enters _answer_parts), because when a deep turn flushed nothing this fallback was
            # showing that interim sentence as the turn's answer: a completion report for work
            # that had not run. When nothing ran because no executor is wired, ``final.text``
            # carries the honest explanation and is what gets shown instead.
            launch_lines = launch_failure_lines(final)
            if launch_lines or (final.kind == "deep" and self._deep_flushed):
                answer = None
            else:
                answer = final.text or ("\n".join(self._answer_parts).strip() or None)
            # The plain "Metric: ..." line a deep result's text ends with is replaced by the metric
            # lines written below (and in each run's record), so it is not printed a second time.
            if answer:
                for deep_result in getattr(final, "deep_results", None) or []:
                    answer = without_line(answer, getattr(deep_result, "metric_results", None) or [],
                                          getattr(deep_result, "metric_missing", False) is True)
            if answer:
                self.write_speaker_label()
                self._console.markdown(answer)
            if launch_lines:
                # Why nothing ran, in plain words, in the transcript: the status line that said it
                # while it happened is gone by now.
                self.write_speaker_label()
                for line in launch_lines:
                    log.write(Text(f"  {line}"))
            s._last_user = user_text
            # For deep runs, signal completion clearly — goal strings look like unfinished TODOs
            if final.kind == "deep":
                deep_results = final.deep_results or []
                goals = final.goals or []
                all_met = bool(deep_results) and all(d.met for d in deep_results)
                goal_str = ("; ".join(goals))[:300] if goals else ""
                # THREE outcomes, not two. With no deep results at all, nothing was even attempted
                # (no executor wired), and recording "Attempted: <goal>" put a false claim into the
                # session history the NEXT turn reads back as context.
                if all_met:
                    prefix = "Completed"
                elif launch_lines:
                    prefix = "Could not start"
                elif deep_results:
                    prefix = "Attempted"
                else:
                    prefix = "Planned but NOT executed (no deep executor configured)"
                s._last_assistant = (f"{prefix}: {goal_str}" if goal_str else f"{prefix}.")
            else:
                # Use _answer_parts as fallback: what was displayed may differ from final.text
                # when the answer streamed via EVENT_PARTIAL or when goal-check text lands in
                # final.text instead of the actual user-facing response.
                s._last_assistant = final.text or "\n".join(self._answer_parts).strip()
            s._session_history.append((user_text, s._last_assistant))
            s._write_session_file()
            s._turn_count += 1
            # A turn that executed work and left some unfinished refreshes the quest folder's
            # standing next-steps artifact. The session owns the decision and the write (shared by
            # every UI); this is only the call site for "a turn just completed".
            s._maybe_refresh_next_steps(final)
            # What the metric was before and after, for any goal whose record did not already show it
            # (a goal that was not met leaves no milestone), written from the result's own data.
            self.write_metrics(getattr(final, "metric_results", None), getattr(final, "metric_missing", False))
            log.write(Text(""))
            # If the deep run flagged the context it used, load the panel so Alt+C opens it.
            # Shallow turns don't get a future_context bullet list from the orchestrator, but they
            # do gather context cards (shown live in the ContextPanel): the same panel serves them,
            # instead of letting the cards be discarded. The closing line says the keys are there.
            context_bullets = self._future_context or _build_shallow_context_bullets(self._ctx.cards)
            if context_bullets:
                self._future_ctx_panel.load(context_bullets)
            hints = []
            if any(run_id in self._deep_archive for run_id in self._deep_flushed):
                hints.append("Alt+D every action")
            if context_bullets:
                hints.append("Alt+C context used")
            self._write_footer(final, elapsed, hints)
            # "Explain how I got this" (see core/answer_explanation.py), when QAR_EXPLAIN_ANSWER is
            # on and the turn passed the model-free eligibility gate. The terminal has no expandable
            # panel to put the full six-section account behind, so this is deliberately just the
            # trace-recorded summary line (source/read/action counts), the same "what did this turn
            # actually touch" idea the Context panel above already shows, not the model-written
            # prose sections (those are the chat surfaces' job).
            explanation = getattr(final, "explanation", None)
            if explanation:
                used = explanation.get("used") or {}
                bits: List[str] = []
                n_sources = len(used.get("cards") or []) + len(used.get("sources") or [])
                n_reads = len(used.get("reads") or [])
                n_actions = len(used.get("actions") or [])
                n_metrics = len(explanation.get("metrics") or [])
                if n_sources:
                    bits.append(f"{n_sources} source{'s' if n_sources != 1 else ''}")
                if n_reads:
                    bits.append(f"{n_reads} read{'s' if n_reads != 1 else ''}")
                if n_actions:
                    bits.append(f"{n_actions} action{'s' if n_actions != 1 else ''}")
                if used.get("web"):
                    bits.append("web search")
                if n_metrics:
                    bits.append(plural(n_metrics, "metric result"))
                if bits:
                    log.write(Text(f"  Explain how I got this: {', '.join(bits)}", style="dim"))
                # The metric lines of the payload (version 3), when this turn's transcript does not hold
                # them already.
                self.write_metrics(explanation.get("metrics"), explanation.get("metric_missing"))
            self.write_pending_question(final)

        log.write(Text(""))
        self._console.rule()
        log.write(Text(""))

        self._turn_active = False
        self._activity.set_queued(0)
        inp = self.query_one("#prompt", PromptTextArea)
        # A decision question leaves the prompt awaiting the user's reply; don't clobber that
        # placeholder with the normal one until they actually answer it.
        inp.set_class(bool(self._awaiting_decision), "-awaiting")
        if self._awaiting_decision:
            inp.placeholder = _AWAITING_DECISION_PLACEHOLDER
        else:
            inp.placeholder = _READY_PLACEHOLDER
        inp.focus()

        # Anything the user typed and queued while this turn ran, and that the orchestrator's own
        # goal loop never got a chance to drain and act on mid-run, becomes the next turn NOW --
        # never left sitting in the transcript marked "queued" with nothing done about it. An
        # Escape cancel (``cancelled=True``) reaches this exact path immediately, since the run
        # it interrupted stops here rather than running to its own natural end. Multiple queued
        # messages are sent as ONE turn, combined in the order they were typed, instead of being
        # replayed one at a time.
        if error is None:
            queued = self._drain_queued_inputs()
            if queued:
                combined = "\n\n".join(queued)
                log.write(Text(
                    f"  ↑ Sending {len(queued)} queued message"
                    f"{'s' if len(queued) != 1 else ''}…",
                    style="bold yellow",
                ))
                log.write(Text(""))
                self._begin_turn(combined, echo=True, auto=False)
                return

        # Auto-execute a planned-but-unexecuted deep turn: once at most (MAX_AUTO_PASSES counts the
        # person's own pass), and never when something other than "nothing ran" is why it did not
        # run (``allows_second_pass``), with the reason said.
        if not cancelled and error is None and final is not None and self._auto_pass < MAX_AUTO_PASSES:
            if self._maybe_handle_deep_plan(final, run=True):
                log.write(Text(
                    "  ↻ Nothing ran the first time, so I'm trying once more.",
                    style="bold yellow",
                ))
                log.write(Text(""))
                self._begin_turn(
                    "Execute it. No more planning, just code it and apply changes now.",
                    echo=False, auto=True, request=self._turn_request,
                )

    def _maybe_handle_deep_plan(self, final, run: bool = False) -> bool:
        """Return True if this is a deep turn that planned work but didn't execute.

        When ``run`` is False, also prints the planned changes (once).
        """
        if getattr(final, "kind", None) != "deep":
            return False
        goals = final.goals or []
        if not goals:
            return False
        if not allows_second_pass(final):
            return False
        executed = any(
            getattr(d, "met", False) or (getattr(d, "output", "") or "").strip()
            for d in (final.deep_results or [])
        )
        if executed:
            return False
        if not getattr(self.sess._cfg, "deep_runner", None):
            # No executor at all. Say so only when the result does not already explain itself:
            # a kind="deep" result from an unwired session now carries the explanation as its own
            # text (shown as the turn's answer just below), and repeating it as a dim side note
            # would just say the same thing twice.
            if not run and not (getattr(final, "text", "") or "").strip():
                self._console.dim("  (No deep executor configured; cannot auto-execute)")
            return False
        if not run and not getattr(self, "_deep_plan_shown", False):
            self._console.dim("  Task identified. Planned changes:")
            for i, g in enumerate(goals, 1):
                prefix = "▸ " if i == 1 else "  "
                self._console.dim(f"  {prefix}{g}")
            self._deep_plan_shown = True
        return True

    def _record_turn(self, final, elapsed: float) -> None:
        s = self.sess
        user = s._last_user or ""
        tok_in = getattr(final, "tokens_in", 0) or 0
        tok_out = getattr(final, "tokens_out", 0) or 0
        s._turns.append({
            "user": (user[:60] + "…") if len(user) > 60 else user,
            "model": _model_label(getattr(final, "model", None)),
            "tokens_in": tok_in,
            "tokens_out": tok_out,
            "elapsed": elapsed,
            "timestamp": time.time(),
        })
        self._deep_plan_shown = False

    def write_speaker_label(self, force: bool = False) -> None:
        """The AI's name above what it says: once per reply, or again (``force``) for a new message."""
        if self._ai_label_shown and not force:
            return
        self._tlog.write(Text(""))
        self._tlog.write(Text(speaker_label(self.rep_name), style="bold cyan"))
        self._ai_label_shown = True

    def write_provider_error(self, error: Exception) -> None:
        """A failed model call in plain words, with the way forward, and the message put back to retry.

        The failed message goes back into the prompt box when the box is empty, so Enter sends it
        again; a box the person already typed in is left alone and the text says to send it again."""
        inp = self.query_one("#prompt", PromptTextArea)
        restored = bool(self.retry_text) and not inp.text.strip()
        if restored:
            inp.text = self.retry_text
        self.write_speaker_label(force=True)
        for line in provider_error_lines(error, message_restored=restored):
            self._tlog.write(Text(f"  {line}", style="yellow"))

    def write_pending_question(self, final) -> None:
        """End a turn that waits for the person's answer on the question itself, from its speaker.

        The last thing on screen, so the prompt below it is answering something in view. The words
        are the question as the orchestrator raised it (an event, or the confirm result's own
        ``question``); a turn that waits with no question text (a parked approval) writes nothing."""
        waiting = turn_outcome(final) == "waiting"
        question = (self.pending_question or getattr(final, "question", None) or "").strip()
        if not waiting and not question:
            return
        self._awaiting_decision = True
        if not question:
            return
        self.write_speaker_label(force=True)
        # Visually distinct (a yellow bar) so a question the AI needs answered before it can
        # continue does not blend into the rest of the transcript.
        for line in question.splitlines():
            self._tlog.write(Text(f"  ┃ {line}" if line.strip() else "  ┃", style="bold yellow"))

    def _write_footer(self, final, elapsed: float, hints: Sequence[str] = ()) -> None:
        """The turn's one quiet closing line: how it ended, how long, the model, and the keys that
        open more (``hints``). A deep turn, or one waiting for the person, says its outcome in words
        of its own (finished, stopped, paused, waiting); a plain answer keeps its counts."""
        def _k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        parts: List[tuple] = []   # (text, style)
        verbose = self.verbosity >= 1
        outcome = turn_outcome(final)
        deep_results = final.deep_results or [] if final.kind == "deep" else []

        if outcome is not None:
            text, style = closing_text(outcome, limit_reset(final) if outcome == OUTCOME_LIMIT else "")
            took = format_elapsed(elapsed)
            if outcome == OUTCOME_FINISHED:
                parts.append((f"{text} in {took}", style))
            else:
                parts.append((text, style))
                parts.append((took, "dim"))
            if verbose:
                deep_tokens = sum(getattr(d, "tokens", 0) for d in deep_results)
                if deep_tokens:
                    parts.append((f"{_k(deep_tokens)} tokens", "dim"))
        else:
            steps = getattr(final, "steps", 0)
            if steps:
                parts.append((f"{steps} step{'s' if steps != 1 else ''}", "dim"))
            src = self._ctx.total_sources
            if src:
                parts.append((f"{src} source{'s' if src != 1 else ''}", "dim"))
            if self._ctx.replans:
                parts.append((f"{self._ctx.replans} replan{'s' if self._ctx.replans != 1 else ''}", "dim"))
            tok_in = getattr(final, "tokens_in", 0) or 0
            tok_out = getattr(final, "tokens_out", 0) or 0
            if tok_in or tok_out:
                parts.append((f"↥ {_k(tok_in)} in · ↦ {_k(tok_out)} out", "dim"))

        if final.kind == "deep":
            models = [(getattr(d, "model", None), getattr(d, "resolved_model", None))
                      for d in deep_results if getattr(d, "model", None)]
        else:
            models = [(getattr(final, "model", None), None)]
        model_lbl = model_text(models, verbose)
        if model_lbl:
            parts.append((model_lbl, "dim"))
        if outcome is None:
            parts.append((f"{elapsed:.1f}s", "dim"))
        for hint in hints:
            parts.append((hint, "dim"))

        # One space around the dot on a closing line (hints and all must fit one 80 column row).
        separator = " · " if outcome is not None else "  ·  "
        t = Text("  ")
        for i, (label, style) in enumerate(parts):
            if i > 0:
                t.append(separator, style="dim")
            t.append(label, style=style)
        self._tlog.write(t)
