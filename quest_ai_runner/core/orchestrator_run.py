"""The per-request loop (``run``) and its streaming wrapper.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.

``run`` itself only builds the run-local namespace ``rs`` and calls the phases in order; each phase is
a method of one of the role mixins below (``orchestrator_run_begin``, ``_loop``, ``_answer``,
``_close``) and returns a terminal ``OrchestratorResult`` to end the turn early, or ``None`` to go on.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from .adapters_events import Mode
from .adapters_deep import ProgressSink
from .orchestrator_run_answer import RunAnswerMixin
from .orchestrator_run_begin import RunBeginMixin
from .orchestrator_run_close import RunCloseMixin
from .orchestrator_run_loop import RunLoopMixin
from .orchestrator_types import OrchestratorResult


class RunMixin(RunBeginMixin, RunLoopMixin, RunAnswerMixin, RunCloseMixin):
    """The per-request loop (``run``) and its streaming wrapper."""

    # --- the loop ------------------------------------------------------------

    def run(self, user_message: str, *, transcript: str = "", context_view: str = "",
            quest_id: Optional[str] = None,
            mode: Mode = Mode.LIVE,
            sink: Optional[ProgressSink] = None,
            background_sink: Optional[ProgressSink] = None,
            detach_check: Optional[Callable[[], bool]] = None,
            model_hint: Optional[str] = None,
            attachments: Optional[List[Dict[str, Any]]] = None,
            context_meta: Optional[Dict[str, Any]] = None,
            rep_preamble: Optional[str] = None,
            working_dir_override: Optional[str] = None,
            pending_inputs: Optional[Callable[[], List[str]]] = None,
            conv_id: Optional[str] = None,
            conv_scope: Optional[Dict[str, Any]] = None,
            prior_escalations: Optional[List[Dict[str, Any]]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
            card_thread: Optional[Any] = None,
            prior_narration: Optional[List[str]] = None,
            anticipated_id: Optional[str] = None,
            message_is_user_turn: bool = True,
            resume_session_id: Optional[str] = None,
            now: Optional[str] = None,
            run_control: Optional[Any] = None,
            deep_goal: Optional[str] = None) -> OrchestratorResult:
        """Run the bounded loop for one request and return a terminal OrchestratorResult.

        Streaming/event interface (both lanes use the SAME emissions; the SINK decides policy):

        * ``mode``        — ``Mode.LIVE`` (attended; stream everything) or ``Mode.BACKGROUND``
                            (sent-off; surface only result/decision/milestone). Default LIVE.
        * ``sink``        — a ``ProgressSink`` the orchestrator routes every internal event
                            through. For LIVE pass a ``StreamSink``; for BACKGROUND a
                            ``MilestoneSink``. If ``None``, only the legacy ``status`` callback
                            fires (a plain non-streaming run, unchanged for old callers).
        * ``background_sink`` + ``detach_check`` — the LIVE↔BACKGROUND handoff. If a LIVE run is
                            abandoned (``detach_check()`` returns True, e.g. the consumer
                            disconnected), the run CONTINUES to completion and the REST of its
                            events (incl. the final result) are delivered via ``background_sink``
                            (a ``MilestoneSink``) instead of the dropped live stream.
        * ``model_hint``  — optional opaque model/tier string carried by the task or caller
                            (e.g. from ``task.model``). When set, it overrides the planner's own
                            tier choice and the compile-time default for answer and deep steps.
                            The consumer's ``ModelProvider`` and ``ModelRegistry`` interpret the
                            string (a tier name or a model id). Unknown values degrade gracefully
                            to the registry's default. Absent/None means exactly today's behavior.
        * ``attachments`` — optional list of in-memory attachment items (chat file uploads AND/OR
                            panel context-docs, the SAME generic shape; see
                            ``core.attachments.prepare_attachments``). Each is a dict
                            ``{filename, mime_type, data: bytes, kind: "image"|"file"}``. The
                            runner OWNS multimodal (the text provider does not): images go NATIVELY
                            to the answer when the answering model/provider is vision-capable, else
                            they are described to text; non-image files are text-extracted. Their
                            text descriptions/inventory join the PLANNER context (so planning is
                            grounded in them), and native image blocks ride the final ANSWER
                            message. Absent/None means exactly today's behavior.
        * ``rep_preamble`` — optional PER-RUN context preamble for the deep step only (e.g. an AI
                            rep's persona + learned corrections, so a deep run executes AS that
                            rep). The brain stays ignorant of its content — it forwards the string
                            to a deep runner that accepts ``context_preamble`` and leaves older
                            runners untouched. Absent/None means exactly today's behavior.
        * ``working_dir_override`` — optional PER-RUN working-directory override for the deep step
                            only (e.g. a quest's synced folder, resolved by the caller from its own
                            quest-folder map). Forwarded to a deep runner that accepts a
                            ``working_dir`` kwarg (see ``core.goal_runner.SubprocessGoalRunner``);
                            older runners are untouched. Absent/None means the deep runner's own
                            configured global working directory applies, exactly as before.

        * ``conv_id``     — optional id of the CURRENT conversation. When set AND a
                            ``conversation_store`` is wired, Step 1 (User Input Understanding) may
                            pull a relevant slice of that conversation to resolve a short/anaphoric
                            message into a self-contained goal condition before context selection.
                            Absent/None (or no store) means Step 1 is a no-op (zero latency).
        * ``conv_scope``  — optional scope dict ({user_id, team_ids, since, participant_id, ...})
                            for finding RELATED past conversations when the current slice is not
                            enough to resolve the message. Interpreted by the ConversationStore.
        * ``prior_escalations`` — optional caller-supplied history of EARLIER turns in this SAME
                            conversation that already escalated (to deep execution or to a human),
                            each e.g. ``{"kind": "deep"|"human", "exit_reason": "...", "outcome":
                            "..."}``. The brain has no persistent storage of its own (core stays
                            domain-free; see ``adapters.ConversationStore`` for the same pattern), so
                            a caller that wants the overseer to see this history across turns passes
                            it forward here (e.g. sourced from the ``exit_reason``/``overseer_signals``
                            of previous ``OrchestratorResult``s it already persisted). Feeds the
                            overseer digest's PRIOR ESCALATIONS THIS CONVERSATION section. Absent/None
                            means "none yet" (today's behavior, byte-for-byte, when overseer is off).
        * ``cancel_check`` -- optional zero-arg callable a caller polls for COOPERATIVE mid-run
                            cancellation (e.g. a runner backed by a task store where a human can
                            cancel a background task while it is in progress). Checked at natural
                            loop boundaries: before starting each new plan/gather/replan step, before
                            each deep-goal retry attempt, and once more after deep execution
                            finishes. When it returns True the run stops cleanly and returns an
                            ``OrchestratorResult`` with ``kind="cancelled"`` (no ``text``/
                            ``deep_results``/``decision_id`` to act on) instead of the normal
                            answer/deep/confirm outcome. It is never called more than the loop's own
                            natural cadence, so a cheap/throttled implementation is fine. Absent/None
                            means exactly today's behavior (a run can never be cancelled mid-flight).
        * ``card_thread`` -- optional PER-IDEA THREADING context (a ``CardThreadContext`` or the
                            equivalent dict; see ``core.card_thread``). Only consulted when the
                            consumer opted in with ``cfg.card_thread_enabled``. It names the card
                            (idea) the conversation is CURRENTLY on plus any candidates the consumer
                            always wants offered; the orchestrator adds the cards its own retrieval
                            already scored this turn (the free PRIOR) and asks the planner, on the
                            call it already makes, which card THIS message belongs to. The resolved
                            assignment comes back as ``OrchestratorResult.card_thread`` and
                            EVENT_CARD_THREAD. The orchestrator persists nothing: the consumer owns
                            the card store and stamps its own messages. Absent/None (or the flag off)
                            means exactly today's behavior.
        * ``prior_narration`` -- optional list of narration lines (most recent last) already spoken
                            aloud to this user in EARLIER turns of this same conversation, when
                            narration is on (``cfg.narrate``). The brain has no persistent storage of
                            its own (same reasoning as ``prior_escalations`` above), so a caller that
                            narrates every turn out loud (e.g. a voice consumer) and wants the ack to
                            stop reopening with its own recent generic "let me look into that" shape
                            passes the last few lines it actually delivered to audio back in here; a
                            reasonable source is ``OrchestratorResult.narration_said`` from the
                            PREVIOUS turn(s), capped by the caller to a handful of lines. Feeds the
                            narrator's repeat-detector and its ack/relay prompts (see ``Narrator``).
                            Absent/None means "no cross-turn memory" (today's behavior).
        * ``resume_session_id`` -- optional id of a deep-worker session an EARLIER run (a previous
                            run of the same task/thread, hours or days ago) left behind. When set,
                            the FIRST deep attempt of this turn resumes that session instead of
                            opening a fresh one, so it starts holding what that run read and
                            decided. It rides the same plumbing as the within-run turn-budget
                            continuation (see ``_run_deep``) rather than a second resume path, and
                            it is consumed ONCE per turn: a later deep call in the same turn starts
                            cold, since the thread has one session to continue, not several.
                            Best effort by contract: a runner that cannot resume ignores it, and a
                            session the worker no longer holds degrades to a cold start rather than
                            failing the run. Absent/None means exactly today's behavior.
        * ``run_control``  -- optional ``core.run_control.RunControl`` for THIS run: the channel a
                            deep worker uses to take steering messages, and to be stopped or
                            paused, while it works. Handed only to a worker that declares a
                            ``control`` keyword and only on a single-goal run. Absent/None means
                            exactly today's behavior.
        * ``deep_goal``    -- optional goal of a deep run the CALLER has already decided on (for
                            example a terminal ``/resume`` of a paused worker). The turn goes straight
                            to a deep run of this goal: no understanding step, no goal-condition
                            call, no turn-start context search and no planner call, so the routing
                            can never turn it into a plain answer, a clarifying question or a hold.
                            ``user_message`` is the brief handed to the worker, and pairs with
                            ``resume_session_id`` to continue a worker's session. A decision made by
                            the caller's code, never read from anyone's words. Absent/None means
                            exactly today's behavior.
        * ``now``          -- optional ISO date/datetime string, the CALLER's notion of "now". Fed
                            into goal-condition/constraint derivation (see ``_derive_goal_condition``)
                            so a relative date in the message ("Wednesday", "last week") resolves
                            against the caller's real clock instead of the server's. Absent/None
                            falls back to the system clock (UTC) -- relative-date resolution still
                            works, just against this process's clock.

        ``run`` still works with NO event args (back-compat: same signature callers used before
        plus keyword-only extras), returning the terminal ``OrchestratorResult``.
        """
        rs = SimpleNamespace(
            user_message=user_message,
            transcript=transcript,
            context_view=context_view,
            quest_id=quest_id,
            mode=mode,
            sink=sink,
            background_sink=background_sink,
            detach_check=detach_check,
            model_hint=model_hint,
            attachments=attachments,
            context_meta=context_meta,
            rep_preamble=rep_preamble,
            working_dir_override=working_dir_override,
            pending_inputs=pending_inputs,
            conv_id=conv_id,
            conv_scope=conv_scope,
            prior_escalations=prior_escalations,
            cancel_check=cancel_check,
            card_thread=card_thread,
            prior_narration=prior_narration,
            anticipated_id=anticipated_id,
            message_is_user_turn=message_is_user_turn,
            resume_session_id=resume_session_id,
            now=now,
            run_control=run_control,
            deep_goal=deep_goal,
        )
        res = self._run_begin(rs)
        if res is not None:
            return res
        res = self._run_gather_context(rs)
        if res is not None:
            return res
        res = self._run_define_finish(rs)
        if res is not None:
            return res
        res = self._run_plan_loop(rs)
        if res is not None:
            return res
        res = self._run_route_action(rs)
        if res is not None:
            return res
        res = self._run_check_answer(rs)
        if res is not None:
            return res
        res = self._run_deferred_work(rs)
        if res is not None:
            return res
        return self._run_verify_answer(rs)

    # --- LIVE streaming convenience: a generator yielding events as they happen --------

    def run_stream(self, user_message: str, *, transcript: str = "", context_view: str = "",
                   quest_id: Optional[str] = None,
                   mode: Mode = Mode.LIVE,
                   model_hint: Optional[str] = None,
                   attachments: Optional[List[Dict[str, Any]]] = None,
                   rep_preamble: Optional[str] = None,
                   pending_inputs: Optional[Callable[[], List[str]]] = None,
                   conv_id: Optional[str] = None,
                   conv_scope: Optional[Dict[str, Any]] = None,
                   now: Optional[str] = None,
                   context_meta: Optional[Dict[str, Any]] = None,
                   cancel_check: Optional[Callable[[], bool]] = None,
                   stop_join_seconds: float = 60.0,
                   resume_session_id: Optional[str] = None,
                   run_control: Optional[Any] = None,
                   deep_goal: Optional[str] = None):
        """Generator form of ``run`` for a LIVE consumer that wants to iterate events.

        ``cancel_check`` is forwarded to ``run`` (cooperative cancel, which also stops a running
        deep worker process). Closing the generator early (``break`` then ``close()``, or letting
        it be garbage collected) also cancels the run and WAITS up to ``stop_join_seconds`` for the
        worker thread to exit, so a consumer that abandons the stream never has a run still working
        behind its back when it starts the next turn.

        Yields each ``ProgressEvent`` (as emitted, post-sink-policy for the given mode) and,
        as the FINAL item, the terminal ``OrchestratorResult``. Implemented by feeding a
        ``StreamSink`` whose forward appends to a thread-safe queue while ``run`` executes in a
        worker thread, so the caller streams in real time and still gets the structured result.

        ``model_hint`` and ``context_meta`` are forwarded to ``run`` unchanged — see ``run`` for
        semantics. So are ``resume_session_id`` and ``run_control`` (the control channel of this
        run, see ``core/run_control.py``): a live consumer such as the terminal steers, stops
        and pauses a running deep worker through them. So is ``deep_goal``, which sends the turn
        straight to a deep run (the terminal's ``/resume``). All default to None, which is today's
        behaviour.

        Example::

            for item in orch.run_stream("..."):
                if isinstance(item, OrchestratorResult):
                    final = item            # terminal result
                else:
                    render(item)            # a dict event
        """
        import queue as _queue
        import threading as _threading

        from .adapters_sinks import StreamSink

        q: "_queue.Queue" = _queue.Queue()
        _SENTINEL = object()
        sink = StreamSink(lambda ev: q.put(("event", ev)))

        result_box: Dict[str, Any] = {}
        abandoned = _threading.Event()

        def _cancelled() -> bool:
            if abandoned.is_set():
                return True
            return bool(cancel_check()) if cancel_check is not None else False

        def _worker():
            try:
                res = self.run(user_message, transcript=transcript, context_view=context_view,
                               quest_id=quest_id, mode=mode, sink=sink, model_hint=model_hint,
                               attachments=attachments, rep_preamble=rep_preamble,
                               pending_inputs=pending_inputs, conv_id=conv_id,
                               conv_scope=conv_scope, now=now, context_meta=context_meta,
                               cancel_check=_cancelled, resume_session_id=resume_session_id,
                               run_control=run_control, deep_goal=deep_goal)
                result_box["result"] = res
            except Exception as e:  # noqa: BLE001
                result_box["error"] = e
            finally:
                q.put(("done", _SENTINEL))

        t = _threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            while True:
                kind, payload = q.get()
                if kind == "event":
                    yield payload
                else:
                    break
        finally:
            if t.is_alive():
                # The consumer stopped early. Cancel the run and wait for the thread, so the
                # worker (and any Claude Code process under it) is stopped before the caller
                # moves on.
                abandoned.set()
                t.join(timeout=stop_join_seconds)
                if t.is_alive():
                    log.warning("run_stream: the run did not stop within %ss of being abandoned",
                                stop_join_seconds)
        t.join()
        if "error" in result_box:
            raise result_box["error"]
        yield result_box.get("result")
