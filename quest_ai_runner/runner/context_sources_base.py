"""The ``ContextSource`` interface and the base class every source builds on.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, Sequence
from datetime import datetime

from esoi import implementation
from .context_update_types import ContextUpdate, _as_utc, log
from .context_update_state import CollectRequest


@implementation("feed-context-sources", "base")
class ContextSource(Protocol):
    """One channel this engine can check. Implementations must never raise; the engine catches
    anyway, but a source that reports its own empty result can say WHY."""

    name: str

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        ...


class _BaseSource:
    """Shared plumbing: a name, a one-line description for ``describe_sources``."""
    name = ""
    describes = ""
    # Whether this source's items should be put to a relevance judgment before they reach a run.
    #
    # The split is CARD-SCOPED vs USER-SCOPED, and it is the whole rule. A note on this quest, or a
    # comment on a document this card owns, is addressed to this work by construction: it is
    # relevant because of WHERE it was written, and no judgment is needed or wanted. A capture the
    # person made on their phone is user-scoped -- it is about whatever was on their mind, which is
    # usually some other part of their life -- so it arrives at every card equally and only some of
    # it bears on any one of them.
    judge_relevance = False
    # Whether this channel carries things somebody is ASKING FOR, as opposed to things they are
    # recording. A note and a document comment are asks; a reflection, a habit log and a capture
    # are a person writing down their own day, and nothing is owed on them.
    #
    # This decides what enters the feedback ledger. Getting it wrong is not cosmetic: a live run
    # tracked the habit log and the daily reflection as asks, and the next morning both came back
    # as "still owed, needs an answer", which is an assistant asking a person to answer their own
    # diary.
    tracks_asks = False
    # Whether this source's items need an ADMISSION judgment before anything else sees them: is
    # this a real person writing to us at all, as opposed to bulk or automated mail that cleared a
    # deterministic header check. A DIFFERENT question from ``judge_relevance`` (which asks
    # whether an item bears on THIS card) and never folded into it -- see
    # ``runner.context_updates.llm_admission_judge`` for why conflating them is wrong. A class
    # attribute, alongside ``judge_relevance``, so a source declares it rather than the engine
    # hardcoding which one gets it: ``InboundMailSource`` is the first, and any future source that
    # reads from a space where non-human senders are possible gets the same gate for free.
    judge_admission = False
    # Whether an admitted item from this source should be posted onto the card it arrived for, as
    # a note, so the account's own asks record (not this library's local feedback ledger, which is
    # a different thing -- see docs/feedback-ledger.md) sees it under the sender's own name. Only
    # a source whose items are things a REAL PERSON outside the account sent in wants this: a
    # quest note or a Drive comment already has its own answer channel and its own place in the
    # product surface, and relaying it as a SECOND note would just be noise.
    relay_to_quest = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:  # pragma: no cover
        raise NotImplementedError


# A run that reached one of these has had its final say, and that say was delivered: it is on the
# quest, rolled onto the pass that created it, and mailed where the quest mails. Mirrors
# ``runner.autopilot``'s own set (``needs_you`` included: a question the run stopped on is exactly
# what the person needs to read).
DELIVERED_TASK_STATUSES = frozenset({"done", "needs_you", "incomplete", "failed"})


def _reply_channel(card: Dict[str, Any]) -> str:
    """Where an answer to this card's people actually reaches them, in one imperative phrase.

    A quest that mails sends the run's RESULT to its people, with a reply address that comes back
    as a note. So on those quests the result is the message, and "add a note on this quest" is
    advice to write somewhere nobody is looking: notes are the record an assistant keeps, not the
    thing a person opens. Where mail is off, the note IS the channel and the phrase says so.
    """
    email = ((card or {}).get("autopilot") or {}).get("email") or {}
    if email.get("enabled"):
        return ("answer it in your result, which is what gets mailed to them; "
                "the note you keep on the quest is the record, not the reply")
    return "add a note on this quest"


def _runs_delivered(request: "CollectRequest") -> List[datetime]:
    """When runs on this card last delivered their results, newest last.

    Read through the shared per-engine cache, so one pass over many cards does not re-list a
    card's tasks per source. A client without ``list_tasks`` degrades to "no deliveries", which is
    exactly the behaviour that existed before answers could come from a result.
    """
    lister = getattr(request.client, "list_tasks", None)
    if not callable(lister) or not request.card_id:
        return []
    key = f"tasks:{request.card_id}"
    if key not in request.cache:
        try:
            request.cache[key] = list(lister(goal_id=request.card_id) or [])
        except Exception as e:  # noqa: BLE001 -- history is context, never a reason to fail
            log.info("context updates: could not read tasks for %s (%s)", request.card_id, e)
            request.cache[key] = []
    out = []
    for t in request.cache[key]:
        if str(t.get("status") or "").strip().lower() not in DELIVERED_TASK_STATUSES:
            continue
        if not str(t.get("result") or "").strip():
            continue
        when = _as_utc(t.get("worked_at") or t.get("updated_at") or t.get("created_at"))
        if when:
            out.append(when)
    out.sort()
    return out


def _notes_explanation(persons: int, offered: int, open_now: int) -> str:
    """Why fewer notes were offered than the person wrote."""
    if not persons:
        return ""
    if offered:
        return ""
    if open_now:
        return f"{persons} note(s), none new since the last delivery"
    return f"{persons} note(s), all already answered"
