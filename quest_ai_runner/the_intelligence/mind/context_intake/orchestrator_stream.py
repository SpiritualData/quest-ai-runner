"""The streaming form of ``run``: a generator yielding events as they happen.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
``run`` (``orchestrator_run``) does the turn; this wraps it in a worker thread behind a
``StreamSink`` so a live consumer (the terminal, the channel runner) iterates the events.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from hubofhubs.api import Mode

LOG = logging.getLogger(__name__)


class StreamMixin:
    """``run_stream``: the generator form of ``Orchestrator.run``."""

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
        run, see ``hubofhubs.ports.control``): a live consumer such as the terminal steers, stops
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

        from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink

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
                    LOG.warning("run_stream: the run did not stop within %ss of being abandoned",
                                stop_join_seconds)
        t.join()
        if "error" in result_box:
            raise result_box["error"]
        yield result_box.get("result")
