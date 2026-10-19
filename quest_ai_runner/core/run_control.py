"""The control channel of ONE deep run: steer, stop, pause.

A person can add direction, stop or pause while a deep worker is running. The caller (the task
executor, a chat) knows how to reach its own store of messages and stop requests; the worker knows
how to put a message in front of its model. ``RunControl`` is the seam between them, handed to ONE
call of ``DeepRunner.run_goal`` (``control=...``), so it is bound to one run and can never leak to
another: two runs in one process hold two controls.

Nothing here is Quest-specific. Every field is a plain callable the caller supplies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class ControlMessage:
    """One message a person sent to a running run: a stable id (so it is delivered once and
    reported as seen by id) and its text."""
    id: str
    text: str


def never() -> bool:
    return False


def no_messages() -> List[ControlMessage]:
    return []


def ignore_seen(ids: List[str], where: str) -> None:
    return None


@dataclass
class RunControl:
    """What a running worker may ask of its caller, and tell it.

    * ``claim_inputs()`` hands over the messages sent since the last call, each ONCE. The worker
      calls it only at the moment it is about to put the result in front of its model, so a
      message that is claimed is a message that is written; a message the worker never asks for
      stays with the caller (a run that is finishing leaves it for a follow-up run).
    * ``mark_seen(ids, where)`` is called when the model has actually received those messages
      (``where`` names the stage, ``"deep_worker"`` for a Claude Code session).
    * ``cancel_requested()`` / ``pause_requested()`` are polled while the run works. Both stop the
      current step and end the run keeping the session; a pause is resumable on the same thread.
    * ``poll_seconds`` is how often the worker looks for new messages (the latency a person waits
      before their message is written into the session).
    Every callable must be cheap and must never raise; the worker also guards each call.
    """
    claim_inputs: Callable[[], List[ControlMessage]] = no_messages
    mark_seen: Callable[[List[str], str], None] = ignore_seen
    cancel_requested: Callable[[], bool] = never
    pause_requested: Callable[[], bool] = never
    poll_seconds: float = 2.0
    # Optional label for logs only (a task id, never used for routing).
    label: Optional[str] = None
