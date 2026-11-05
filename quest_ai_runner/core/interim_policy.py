"""When a turn that is not finished sends an interim message, and what that message holds.

Pure policy, no model call and no I/O (``docs/explanation/incremental-replies-and-asks.md`` section 6.2). Every input
is structural: the verifier's enum verdict, whether the loop really runs again, the run's own receipts and declared
coverage, a clock reading and two counters. Nothing here reads the wording of model output (hard rule #3): the only
text it handles is carried through to the reader, never inspected.

``decide`` answers "send now?" with a reason; ``InterimLedger`` is the per-turn record of what was sent;
``interim_event_data`` builds the event payload, whose ``done`` comes from receipts and coverage only, never from the worker's prose.
"""
from __future__ import annotations

import hashlib
import re
import threading
import typing
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from esoi import implementation, solution
from hubofhubs.the_intelligence.critical_thinking.goal_completion.done_evidence import interim_data, parse_coverage, render_coverage_line

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

    The first two parts (coverage, receipts) say whether work advanced; the digest of the whitespace-normalised output
    says only whether the text differs, and ``decide`` ignores it once a message has been sent.
    """
    record = parse_coverage(coverage)
    covered = (record.processed, record.total) if record is not None else None
    lines = tuple(str(r).strip() for r in receipts if str(r or "").strip())
    flat = " ".join(str(output or "").split())
    digest = hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16] if flat else ""
    return (covered, lines, digest)


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
    # NEW since the last message means a coverage advance or a new receipt. The worker's own text is a delta only for the FIRST message:
    # after that a reworded account of the same work is not news (a real turn sent four near-identical tables, 2026-11-01), and the
    # Trail already shows live work. The text's digest stays in the signature for the first comparison and for the ledger.
    if state.last_signature is not None and state.signature[:2] == state.last_signature[:2]:
        return InterimDecision(False, SKIP_NO_DELTA)
    if state.sent_count >= max(0, state.max_per_turn):
        return InterimDecision(False, SKIP_CAP)
    if state.last_sent_at is not None and (state.now - state.last_sent_at) < state.min_seconds_between:
        return InterimDecision(False, SKIP_TOO_SOON)
    return InterimDecision(True, SEND)


EXAMPLE_STATE = InterimState(enabled=True, met=False, blocker="more_work", loop_runs_again=True, ask_open=False, has_why=True,
                             has_content=True, sent_count=0, last_sent_at=None, now=0.0, signature=(None, ("saved the outline",), ""),
                             last_signature=None)


@solution("reply-in-steps", kind="event", entry="decide", example=EXAMPLE_STATE)
@typing.runtime_checkable
class InterimPolicy(typing.Protocol):
    """Says whether a turn that is not finished sends an interim message now, and why."""

    def decide(self, state: InterimState) -> InterimDecision: ...


@implementation("reply-in-steps", "gated")
class GatedInterim:
    """The gated policy: ``decide`` above, offered as an object (the only arm that is built; no state, no model call)."""
    REQUIRES = ()     # a pure function of the state

    def decide(self, state: InterimState) -> InterimDecision:
        return decide(state)


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


def done_text(*, receipts: Iterable[str], coverage: Any) -> str:
    """What was done, for the reader: the runner's own receipts (counts and titles from tool results), then the declared coverage line.

    The worker's own prose is NEVER part of it (Joshua's rule, after a real turn repeated per-frequency figures the worker had derived
    from a table it could not verify): a claim or number reaches the reader only through a receipt the run holds. A worker that
    reports no receipts and no coverage gives an empty ``done``, and ``decide`` then sends nothing (the Trail still shows the work).
    """
    parts: List[str] = [f"- {str(r).strip()}" for r in receipts if str(r or "").strip()]
    line = render_coverage_line(coverage)
    if line:
        parts.append(line)
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


NUMBER_WORDS = frozenset("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
                         "eighteen nineteen twenty thirty forty fifty hundred thousand half double triple".split())
FIGURE = re.compile(r"\d+(?:[.,]\d+)?")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def ungrounded_terms(text: str, allowed: str) -> List[str]:
    """Figures, number words and name-like words in ``text`` that are not in ``allowed`` (the request, the receipts, the coverage line).

    An overlap test against stored text, not a reading of what the text means (hard rule #3): a digit run, a number word, or a
    capitalised word that is not the first of its sentence must each occur in the stored text, or it is reported. Empty means
    every figure and name the message carries came from the request or the run's own record."""
    store = (allowed or "").lower()
    found: List[str] = [m for m in FIGURE.findall(text or "") if m not in store]
    for sentence in re.split(r"(?<=[.!?:])\s+", text or ""):
        for i, word in enumerate(WORD.findall(sentence)):
            low = word.lower()
            if low in NUMBER_WORDS and low not in store.split() and low not in re.findall(r"[a-z]+", store):
                found.append(word)
            elif i > 0 and len(word) > 1 and word[0].isupper() and low not in store:
                found.append(word)
    return found
