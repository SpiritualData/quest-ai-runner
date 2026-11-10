"""Resuming a run that ended at a limit: recognise the person's Resume and continue the same session instead of starting over.

Quest's Resume is an ordinary message on the task thread or in the chat (the Quest screens send a short fixed sentence), so nothing in the message says
which session to continue. The session comes from the run that ended: its ``resume_handle`` (``run_facts.end_facts``), which a task also reports as its
session and Quest hands back on the next run (``resume_session_id``), and which a chat turn leaves in the ``ResumeLedger`` below. ``is_resume_request``
reads only the person's own short message, never anything a model wrote (hard rule #3); a consumer whose screens say it differently adds its sentences
to ``RESUME_REQUESTS``. A resume with no handle is not claimed: the run then starts as any follow-up does, told how the earlier run ended. Standard library."""
from __future__ import annotations

import re
import threading
from esoi import implementation
from typing import Any, Dict, Iterable, Mapping, Optional

RESUME_REQUESTS = ("continue", "resume", "please continue", "continue please", "please resume", "keep going", "go on",
                   "please continue from where you stopped", "continue from where you stopped", "resume from where you stopped")
MAX_RESUME_WORDS = 8
LEDGER_SIZE = 500


def is_resume_request(text: Any, extra: Iterable[str] = ()) -> bool:
    """True when the whole message is one of the short sentences that ask to continue what stopped."""
    words = re.sub(r"[^\w\s']", " ", str(text or "").casefold()).split()
    if not words or len(words) > MAX_RESUME_WORDS:
        return False
    line = " ".join(words)
    return line in RESUME_REQUESTS or line in {" ".join(re.sub(r"[^\w\s']", " ", str(e).casefold()).split()) for e in extra}


def resume_note(left: str = "", end_reason: str = "") -> str:
    """What the continued session is told: the person asked it to go on, so it finishes what was left and does not redo what is done."""
    parts = ["RESUME (asked by the person): continue this same session from where it stopped. The work already done stands; do not redo it."]
    if end_reason:
        parts.append(f"It stopped because: {end_reason.replace('_', ' ').replace(':', ', ')}.")
    if left:
        parts.append(f"What was left: {left}")
    return " ".join(parts)


@implementation("receive-input-mid-run", "resume_session")
class ResumeLedger:
    """For each chat conversation (or any key), the session a run that did not finish left to continue, with what was left. In memory and bounded: a
    restart forgets it, and the chat then starts the follow-up as any other (Quest's own copy of the handle, on the stored reply, is the durable one)."""
    REQUIRES = ()

    def __init__(self, size: int = LEDGER_SIZE):
        self.size = size
        self.rows: Dict[str, Dict[str, str]] = {}
        self.lock = threading.Lock()

    def note(self, key: Optional[str], facts: Mapping[str, Any]) -> None:
        """Remember the end of a run under ``key``: its resume handle when it has one, else forget (a finished run leaves nothing to resume)."""
        if not key:
            return
        with self.lock:
            handle = str(facts.get("resume_handle") or "")
            if not handle:
                self.rows.pop(key, None)
                return
            self.rows.pop(key, None)
            self.rows[key] = {"handle": handle, "left": str(facts.get("left") or ""), "end_reason": str(facts.get("end_reason") or "")}
            while len(self.rows) > self.size:
                self.rows.pop(next(iter(self.rows)))

    def take(self, key: Optional[str]) -> Optional[Dict[str, str]]:
        """The row of ``key``, once: a resume consumes it."""
        if not key:
            return None
        with self.lock:
            return self.rows.pop(key, None)


LEDGER = ResumeLedger()
