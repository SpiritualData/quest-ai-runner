"""When a blocked turn becomes an ask, who it goes to, and how a turn that stopped without one ends.

Pure decisions plus one call to the wired ``EscalationSink`` (``docs/explanation/incremental-replies-and-asks.md``
section 6.3). Everything is structural: the verifier's ``blocker`` enum, the call ledger's status codes, the
participants list the consumer supplied. Nothing reads the wording of model output (hard rule #3).

* ``blocks_on_person``: a ``needs_person`` verdict with a question, or a tool failure whose ledger status is an
  access refusal a person can fix (401, 403). A platform fault (5xx and the rest) is not an ask.
* ``resolve_assignee``: the asker unless the verifier named a participant the code can resolve; unresolved falls back.
* ``file_ask``: one deduplicated decision-request, or an honest "could not be filed" (never a pretend ask).
* ``stop_reason_of``: how a turn that ended unmet without an ask is described (a final, never an ask).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .adapters_values import Escalation
from .tools_quest_ops import resolve_member

log = logging.getLogger(__name__)

PERSON_FIXABLE_STATUSES = frozenset({"401", "403"})

STOP_BUDGET = "budget"
STOP_STALLED = "stalled"
STOP_ATTEMPT_CAP = "attempt_cap"
STOP_TOOL_FAULT = "tool_fault"


def failure_status(signature: str) -> str:
    """The HTTP-style status inside a ledger failure signature (``tool|status|first sentence``), or ""."""
    parts = str(signature or "").split("|")
    return parts[1] if len(parts) > 2 else ""


def person_fixable(failures: Sequence[Any]) -> bool:
    """True when every unrecovered failure in the ledger window is an access refusal (401 or 403)."""
    rows = list(failures)
    return bool(rows) and all(failure_status(getattr(f, "signature", "")) in PERSON_FIXABLE_STATUSES for f in rows)


def blocks_on_person(blocker: str, question: str, failures: Sequence[Any] = ()) -> bool:
    """Whether the verdict is a block only a person can lift, so an ask is right."""
    if not str(question or "").strip():
        return False
    if blocker == "needs_person":
        return True
    return blocker == "tool_failure" and person_fixable(failures)


@dataclass(frozen=True)
class Assignee:
    user_id: str                  # who the ask goes to ("" when no asker is known)
    asked_of: str                 # "asker" | "member"
    fell_back: bool = False       # the verifier named someone the code could not resolve, so it went to the asker


def resolve_assignee(asked_of: str, member_ref: str, participants: Sequence[Mapping[str, Any]], asker_user_id: str) -> Assignee:
    """The asker by default. A named teammate only when ``asked_of`` is ``member`` and ``member_ref`` resolves to exactly
    one participant (``participants`` are the humans of the conversation who are team members, given by the consumer)."""
    asker = str(asker_user_id or "")
    if str(asked_of or "").strip().lower() != "member" or not str(member_ref or "").strip():
        return Assignee(asker, "asker")
    user_id, _why = resolve_member(member_ref, [dict(p) for p in participants or []])
    if user_id and user_id != asker:
        return Assignee(user_id, "member")
    if user_id == asker:
        return Assignee(asker, "asker")
    return Assignee(asker, "asker", fell_back=True)


def ask_dedupe_key(turn_id: str, conv_id: str, gap: str) -> str:
    """One open ask per (turn, gap): the same gap on a later attempt of the same turn reuses the open ask."""
    flat = " ".join(str(gap or "").lower().split())
    digest = hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16]
    return f"turn-ask:{turn_id or conv_id or 'none'}:{digest}"


@dataclass
class AskOutcome:
    decision_id: str = ""
    assignee: Optional[Assignee] = None
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def filed(self) -> bool:
        return bool(self.decision_id)


def file_ask(sink: Any, *, question: str, why_only_you: str, done: str, options: Optional[List[str]], quest_id: Optional[str],
             conv_id: str, turn_id: str, asker_user_id: str, assignee: Assignee, summary: str) -> AskOutcome:
    """File ONE decision-request through the sink. ``decision_id`` is empty when it could not be filed; the caller then
    ends the turn as a final that says the question could not be filed, never as one that claims an ask exists."""
    if sink is None:
        return AskOutcome(error="no escalation sink is wired")
    try:
        decision_id = sink.escalate(Escalation(
            summary=summary, kind="answer", quest_id=quest_id, assignee=assignee.user_id or None,
            default_on_silence="hold", dedupe_key=ask_dedupe_key(turn_id, conv_id, question), conv_id=conv_id or None,
            notify_user_id=asker_user_id or None, options=list(options) if options else None,
            why_only_you=why_only_you or None))
    except Exception as e:  # noqa: BLE001 -- a failed filing must end the turn honestly, not break it
        log.warning("ask-on-block: the sink raised %s: %s", type(e).__name__, e)
        return AskOutcome(error=f"{type(e).__name__}: {e}")
    decision_id = str(decision_id or "")
    if not decision_id:
        return AskOutcome(error="the sink returned no decision id")
    return AskOutcome(decision_id=decision_id, assignee=assignee)


def ask_event_data(*, question: str, why_only_you: str, done: str, options: Optional[List[str]], outcome: AskOutcome,
                   asker_user_id: str) -> Dict[str, Any]:
    """The ``data`` of the ``EVENT_DECISION`` event for a block (section 10). ``assigned_to_name`` is the consumer's to add."""
    assignee = outcome.assignee or Assignee(asker_user_id, "asker")
    return {"outcome": "asked", "question": question, "why_only_you": why_only_you, "done": done,
            "options": list(options or []), "assigned_to": assignee.user_id, "asked_of": assignee.asked_of,
            "assignee_fell_back": assignee.fell_back, "asker_user_id": asker_user_id}


def ask_text(done: str, question: str) -> str:
    """Plain text of an ask for a consumer that shows only text: what was done, then the one question."""
    head = str(done or "").strip()
    line = f"I need one thing from you to finish this: {str(question).strip()}"
    return f"{head}\n\n{line}" if head else line


def stop_reason_of(*, exhausted_budget: bool, stalled: bool, tool_failure: bool, attempts_spent: bool) -> str:
    """How a turn that ended unmet without an ask is described. Order: a platform fault first (it explains the rest),
    then budget, then stalled, then the attempt cap. All inputs are the loop's own flags."""
    if tool_failure:
        return STOP_TOOL_FAULT
    if exhausted_budget:
        return STOP_BUDGET
    if stalled:
        return STOP_STALLED
    return STOP_ATTEMPT_CAP if attempts_spent else ""
