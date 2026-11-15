"""The control channel every interface gets from its input inbox: a note typed while a run works reaches the live worker, or the next attempt, and is never lost.

The terminal and the task executor build their own ``RunControl``. An interface that only pushes to the inbox (the Quest chat) had none, so a note waited for the end of the attempt
and a worker that finished while a note was in flight took it without ever writing it to its model. ``PendingNotes`` is built from the ``pending_inputs`` callable alone:
``run_control`` hands the notes to a live worker as it asks, ``between_attempts`` gives the next attempt what the last worker took and never confirmed, then anything new.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from hubofhubs.api import ControlMessage, RunControl


class PendingNotes:
    def __init__(self, pending_inputs: Callable[[], List[str]], cancel_check: Optional[Callable[[], bool]] = None, poll_seconds: float = 2.0) -> None:
        self.pending_inputs, self.cancel_check, self.poll_seconds = pending_inputs, cancel_check, poll_seconds
        self.lock = threading.Lock()
        self.counter = 0
        self.unconfirmed: Dict[str, str] = {}

    def claim_inputs(self) -> List[ControlMessage]:
        try:
            texts = [str(t).strip() for t in (self.pending_inputs() or []) if str(t).strip()]
        except Exception:  # noqa: BLE001 -- polling never breaks the run
            return []
        out: List[ControlMessage] = []
        with self.lock:
            for text in texts:
                self.counter += 1
                message = ControlMessage(id=f"note-{self.counter}", text=text)
                self.unconfirmed[message.id] = text
                out.append(message)
        return out

    def mark_seen(self, ids: List[str], where: str) -> None:
        with self.lock:
            for note_id in ids:
                self.unconfirmed.pop(note_id, None)

    def run_control(self) -> RunControl:
        return RunControl(claim_inputs=self.claim_inputs, mark_seen=self.mark_seen, cancel_requested=self.cancel_check or (lambda: False), poll_seconds=self.poll_seconds, label="inbox")

    def between_attempts(self) -> List[str]:
        """Called with no worker live: the notes the last worker took and never confirmed, in the order typed, then anything new."""
        with self.lock:
            texts = [text for _id, text in sorted(self.unconfirmed.items(), key=lambda kv: int(kv[0].rsplit("-", 1)[1]))]
            self.unconfirmed.clear()
        try:
            texts += [str(t).strip() for t in (self.pending_inputs() or []) if str(t).strip()]
        except Exception:  # noqa: BLE001
            pass
        return texts
