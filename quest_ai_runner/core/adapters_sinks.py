"""Ready-made progress sinks: stream, milestone and fan-out.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from esoi import implementation
from .adapters_values import ProgressEvent
from .adapters_events import (
    EVENT_CONTEXT,
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_MILESTONE,
    EVENT_OVERSEER,
    EVENT_RESULT,
    EVENT_TOKENS,
    Mode,
    SURFACING_EVENTS,
)
from .adapters_deep import ProgressSink
from .adapters_bases import ProgressSinkBase


# ---------------------------------------------------------------------------
# The two reference ProgressSinks. The surfacing RULE lives HERE (once), so every
# consumer inherits identical messaging policy regardless of how they wire things.
# ---------------------------------------------------------------------------

@implementation("show-progress", "stream")
class StreamSink(ProgressSinkBase):
    """LIVE sink — forward EVERYTHING to the consumer's stream in real time.

    Construct with a ``forward`` callable (e.g. a websocket/SSE push, or appending to a list
    in tests). It is called with each event's ``dict`` form. A failing forward never breaks
    the run — the orchestrator's work continues even if the stream drops.
    """

    def __init__(self, forward: Callable[[Dict[str, Any]], None]):
        self._forward = forward

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        try:
            self._forward(event.to_dict())
        except Exception:  # noqa: BLE001 — a dropped stream must not break the run
            pass


@implementation("show-progress", "milestone")
class MilestoneSink(ProgressSinkBase):
    """BACKGROUND sink — surface ONLY result / decision / milestone / context / done.

    Planning, reading, re-planning, status ticks, and partial chunks are dropped: nobody is
    attending, so intermediate chatter is noise. The same surfacing set (``SURFACING_EVENTS``)
    is enforced here once. Construct with optional callables for each surfacing kind; the
    runner's executor wires these to (optional progress note / Quest decision-request / final
    PATCH). Any unset callable means "that kind is simply not forwarded anywhere."
    """

    def __init__(
        self,
        *,
        on_context: Optional[Callable[[ProgressEvent], None]] = None,
        on_milestone: Optional[Callable[[ProgressEvent], None]] = None,
        on_decision: Optional[Callable[[ProgressEvent], None]] = None,
        on_result: Optional[Callable[[ProgressEvent], None]] = None,
        on_done: Optional[Callable[[ProgressEvent], None]] = None,
        on_tokens: Optional[Callable[[ProgressEvent], None]] = None,
        on_overseer: Optional[Callable[[ProgressEvent], None]] = None,
    ):
        self._on_context = on_context
        self._on_milestone = on_milestone
        self._on_decision = on_decision
        self._on_result = on_result
        self._on_done = on_done
        self._on_tokens = on_tokens
        self._on_overseer = on_overseer

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        if event.type not in SURFACING_EVENTS:
            return  # drop planning/reading/re-planning/status/partial chatter
        try:
            if event.type == EVENT_CONTEXT and self._on_context:
                self._on_context(event)
            elif event.type == EVENT_MILESTONE and self._on_milestone:
                self._on_milestone(event)
            elif event.type == EVENT_DECISION and self._on_decision:
                self._on_decision(event)
            elif event.type == EVENT_RESULT and self._on_result:
                self._on_result(event)
            elif event.type == EVENT_DONE and self._on_done:
                self._on_done(event)
            elif event.type == EVENT_TOKENS and self._on_tokens:
                self._on_tokens(event)
            elif event.type == EVENT_OVERSEER and self._on_overseer:
                self._on_overseer(event)
        except Exception:  # noqa: BLE001 — a sink callback must not break the run
            pass


class FanoutSink(ProgressSinkBase):
    """Route events to one of two sinks, switchable mid-run (the LIVE↔BACKGROUND handoff).

    Starts forwarding to ``live`` (the attended stream). If the consumer detaches, call
    ``detach()`` to switch to ``background`` (a MilestoneSink) for the REST of the run — the
    work continues to completion and its result/decision/done is delivered via the background
    path instead of the dropped stream. Idempotent: once detached, stays detached.
    """

    def __init__(self, live: ProgressSink, background: ProgressSink):
        self._live = live
        self._background = background
        self._detached = False

    @property
    def detached(self) -> bool:
        return self._detached

    def detach(self) -> None:
        """Switch all subsequent events to the background sink."""
        self._detached = True

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        sink = self._background if self._detached else self._live
        # When detached, surface as BACKGROUND so the milestone policy applies to what's left.
        eff_mode = Mode.BACKGROUND if self._detached else mode
        try:
            sink.update(event, eff_mode)
        except Exception:  # noqa: BLE001
            pass
