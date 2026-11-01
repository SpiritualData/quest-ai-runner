"""When a turn that is not finished sends an interim message, and what that message holds.

Pure policy, no model call and no I/O (``docs/explanation/incremental-replies-and-asks.md`` section 6.2). Every input
is structural: the verifier's enum verdict, whether the loop really runs again, the run's own receipts and declared
coverage, a clock reading and two counters. Nothing here reads the wording of model output (hard rule #3): the only
text it handles is carried through to the reader, never inspected.

``decide`` answers "send now?" with a reason; ``InterimLedger`` is the per-turn record of what was sent;
``interim_event_data`` builds the event payload, whose ``done`` comes from receipts and coverage, and shows the
worker's own account only when no claim check flagged it.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from esoi import implementation
from hubofhubs.critique.done_evidence import interim_data, parse_coverage, render_coverage_line

DEFAULT_MAX_PER_TURN = 4
DEFAULT_MIN_SECONDS_BETWEEN = 15.0

# Reasons ``decide`` can give, for the log and the offline cases.
SEND = "send"
SKIP_DISABLED = "disabled"
SKIP_BLOCKER = "blocker_is_not_more_work"
SKIP_NOT_AGAIN = "loop_does_not_run_again"
SKIP_ASK_OPEN = "an_ask_is_open"
SKIP_NO_WHY = "no_reason_to_give"
SKIP_NO_CONTENT = "nothing_to_show"
SKIP_NO_DELTA = "nothing_new_since_last_message"
SKIP_TOO_SOON = "too_soon_after_last_message"
SKIP_CAP = "per_turn_cap_reached"

Signature = Tuple[Any, ...]


@dataclass(frozen=True)
class InterimState:
    """Everything ``decide`` reads. All of it is known at the point the goal loop commits to another attempt."""
    enabled: bool
    met: bool
    blocker: str                       # the verifier's enum: more_work | evidence_only | needs_person | tool_failure
    loop_runs_again: bool              # attempts and budget are left, not stuck, no stop flag set
    ask_open: bool                     # a decision-request is already waiting on a person for this turn
    has_why: bool                      # the verifier gave a reason, so the message can say why more is coming
    has_content: bool                  # there is something to show: receipts, coverage or output
    sent_count: int
    last_sent_at: Optional[float]
    now: float
    signature: Signature               # what exists now (see ``progress_signature``)
    last_signature: Optional[Signature]
    max_per_turn: int = DEFAULT_MAX_PER_TURN
    min_seconds_between: float = DEFAULT_MIN_SECONDS_BETWEEN


@dataclass(frozen=True)
class InterimDecision:
    send: bool
    reason: str


def progress_signature(*, output: str, receipts: Iterable[str], coverage: Any) -> Signature:
    """A comparable snapshot of what exists now: declared coverage, the receipts, and a digest of the output.

    Two snapshots are equal exactly when nothing new happened between them: no coverage advance, no new receipt,
    no new output content. The digest is of the whitespace-normalised text, so reflowing is not news.
    """
    record = parse_coverage(coverage)
    covered = (record.processed, record.total) if record is not None else None
    lines = tuple(str(r).strip() for r in receipts if str(r or "").strip())
    flat = " ".join(str(output or "").split())
    digest = hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16] if flat else ""
    return (covered, lines, digest)


@implementation("reply-in-steps", "gated")
def decide(state: InterimState) -> InterimDecision:
    """Whether to send an interim message now. Every condition of section 6.2 must hold; the first failing one is the reason."""
    if not state.enabled:
        return InterimDecision(False, SKIP_DISABLED)
    if state.met or state.blocker != "more_work":
        return InterimDecision(False, SKIP_BLOCKER)
    if not state.loop_runs_again:
        return InterimDecision(False, SKIP_NOT_AGAIN)
    if state.ask_open:
        return InterimDecision(False, SKIP_ASK_OPEN)
    if not state.has_why:
        return InterimDecision(False, SKIP_NO_WHY)
    if not state.has_content:
        return InterimDecision(False, SKIP_NO_CONTENT)
    if state.last_signature is not None and state.signature == state.last_signature:
        return InterimDecision(False, SKIP_NO_DELTA)
    if state.sent_count >= max(0, state.max_per_turn):
        return InterimDecision(False, SKIP_CAP)
    if state.last_sent_at is not None and (state.now - state.last_sent_at) < state.min_seconds_between:
        return InterimDecision(False, SKIP_TOO_SOON)
    return InterimDecision(True, SEND)


@dataclass
class InterimLedger:
    """What one turn has sent so far. Thread-safe: parallel sub-goals of one turn share it, so the caps are turn-wide."""
    sent_count: int = 0
    last_sent_at: Optional[float] = None
    last_signature: Optional[Signature] = None
    seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, now: float, signature: Signature) -> int:
        """Note one message as sent and return its 1-based sequence number within the turn (the runner's hint;
        the consumer stamps the authoritative ``seq``)."""
        with self.lock:
            self.sent_count += 1
            self.seq += 1
            self.last_sent_at = now
            self.last_signature = signature
            return self.seq


def done_text(*, receipts: Iterable[str], coverage: Any, output: str, output_flagged: bool) -> str:
    """What was done, for the reader. Receipts first (the runner's own record), then the declared coverage line.

    The worker's account (``output``) is added only when no claim check flagged it (``output_flagged`` is the
    structural ``claims_unexecuted`` / ``claim_corrected`` flag): an unverified "fixed" claim reaching the chat is
    the 2026-09-27 incident this rule answers.
    """
    parts: List[str] = [f"- {str(r).strip()}" for r in receipts if str(r or "").strip()]
    line = render_coverage_line(coverage)
    if line:
        parts.append(line)
    account = str(output or "").strip()
    if account and not output_flagged:
        parts.append(account)
    return "\n".join(parts)


def interim_event_data(*, done: str, why_more: str, next_step: str, coverage: Any, attempt: int, seq: int) -> Dict[str, Any]:
    """The ``data`` of an ``EVENT_INTERIM`` event under the section 10 contract (``turn_id`` is the consumer's to stamp)."""
    return interim_data(coverage, attempt=attempt, seq=seq, done=done, why_more=why_more.strip(),
                        next=next_step.strip(), will_continue=True)


def interim_text_of(data: Dict[str, Any]) -> str:
    """The plain-text rendering carried in the event's ``text`` for a consumer that shows only text: what exists
    now, then why more is coming, then what happens next. Code-written; contains no em dash."""
    parts = [str(data.get("done") or "").strip()]
    why = str(data.get("why_more") or "").strip().rstrip(".")
    nxt = str(data.get("next") or "").strip().rstrip(".")
    if why:
        parts.append(f"This is not finished yet: {why}.")
    if nxt:
        parts.append(f"Next: {nxt}.")
    return "\n\n".join(p for p in parts if p)
