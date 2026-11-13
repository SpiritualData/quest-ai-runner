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

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import resolve_member

log = logging.getLogger(__name__)

PERSON_FIXABLE_STATUSES = frozenset({"401", "403"})

STOP_BUDGET = "budget"
STOP_STALLED = "stalled"
STOP_ATTEMPT_CAP = "attempt_cap"
STOP_TOOL_FAULT = "tool_fault"
STOP_NEEDS_ANSWER = "needs_answer"   # the turn needs the person's own answer in the chat (a name nothing placed): not a limit, so nothing to resume
STOP_UNVERIFIED = "unverified"   # a usable reply with part of it not confirmed (the verdict blocker is evidence_only)


def block_type_of(blocker: str) -> str:
    """What the person must supply, from the verifier's structural ``blocker`` enum: an access refusal is ``needs_access``, any other
    block on a person is ``needs_information`` (the verdict does not distinguish a decision from a step, so none is guessed)."""
    return "needs_access" if blocker == "tool_failure" else "needs_information"


def blocked_goal_of(meta: Mapping[str, Any]) -> str:
    """The specific goal the run is working, from the caller's context: ``related_goal_id`` (an AI task's own goal), else ``goal_id``
    when it is not the quest itself (a quest-scoped task carries the quest's id there). "" when the run is not on one goal."""
    quest = str(meta.get("quest_id") or "")
    for key in ("related_goal_id", "goal_id"):
        value = str(meta.get(key) or "").strip()
        if value and value != quest:
            return value
    return ""


def run_for_of(meta: Mapping[str, Any]) -> List[str]:
    """The people the run works for, in order: the task's requester, then its owner (``task_owner_user_id``). A sink routes an ask
    that names no assignee of its own to the first of them that is not the runner's own account."""
    out: List[str] = []
    for key in ("requester_user_id", "task_owner_user_id"):
        value = str(meta.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


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


def why_only_you_of(blocker: str, assignee: Assignee, participants: Sequence[Mapping[str, Any]]) -> str:
    """The one line saying why this person: written by code from structural facts (who the ask is for, which kind of block), never
    the verifier's own reason, which is model prose that can name a platform fault or echo a worker's claim. Empty when no valid
    reason exists (an ask to the asker whose block is a missing answer needs none)."""
    if assignee.asked_of == "member":
        name = next((str(p.get("name") or "").strip() for p in participants if isinstance(p, Mapping)
                     and str(p.get("user_id") or "") == assignee.user_id), "")
        return f"Only {name} can answer this." if name else "Only a teammate in this conversation can answer this."
    if blocker == "tool_failure":
        return "A tool refused access, and only a person can grant it."
    return ""


def ask_dedupe_key(turn_id: str, conv_id: str, gap: str = "") -> str:
    """One open ask per blocked turn: a later attempt of the same turn, or a sub-goal of it, reuses the open ask however its gap is
    worded (a real team turn filed two asks for one block because the two gaps were worded differently, 2026-11-01). ``gap`` is
    kept for callers that pass it and is not part of the key."""
    return f"turn-ask:{turn_id or conv_id or 'none'}"


# What a worker is told after it raised an ask, read from the timing the hub's Judgment Calls gave it (``{mode: sync|async, ...}``).
ASK_WAITS_GUIDANCE = "It waits for the person: continue with the parts that do not depend on the answer."
ASK_BLOCKS_GUIDANCE = "This blocks the work: stop and say so in your result."


def timing_guidance(timing: Optional[Mapping[str, Any]]) -> str:
    """One sentence for the worker on what the ask's timing means for the rest of its run; "" when no timing is known."""
    mode = str((timing or {}).get("mode") or "") if isinstance(timing, Mapping) else ""
    return {"sync": ASK_BLOCKS_GUIDANCE, "async": ASK_WAITS_GUIDANCE}.get(mode, "")


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
             conv_id: str, turn_id: str, asker_user_id: str, assignee: Assignee, summary: str,
             blocked_goal_id: str = "", block_type: str = "", run_for_user_ids: Sequence[str] = ()) -> AskOutcome:
    """File ONE decision-request through the sink. ``decision_id`` is empty when it could not be filed; the caller then
    ends the turn as a final that says the question could not be filed, never as one that claims an ask exists."""
    if sink is None:
        return AskOutcome(error="no escalation sink is wired")
    try:
        decision_id = sink.escalate(Escalation(
            summary=summary, kind="answer", quest_id=quest_id, assignee=assignee.user_id or None,
            default_on_silence="hold", dedupe_key=ask_dedupe_key(turn_id, conv_id, question), conv_id=conv_id or None,
            notify_user_id=asker_user_id or None, options=list(options) if options else None,
            why_only_you=why_only_you or None, blocked_goal_id=blocked_goal_id or None, block_type=block_type or None,
            run_for_user_ids=list(run_for_user_ids) or None))
    except Exception as e:  # noqa: BLE001 -- a failed filing must end the turn honestly, not break it
        log.warning("ask-on-block: the sink raised %s: %s", type(e).__name__, e)
        return AskOutcome(error=f"{type(e).__name__}: {e}")
    decision_id = str(decision_id or "")
    if not decision_id:
        judged = getattr(sink, "last_judgment", None)
        if isinstance(judged, Mapping) and judged.get("decision") in ("proceed", "decline"):
            # the hub's Judgment Calls, under this person's own policy: said plainly, so the turn never claims an ask exists
            return AskOutcome(error=f"not filed: Judgment Calls decided {judged['decision']} for this person"
                                    + (f" ({judged['reason']})" if judged.get("reason") else ""))
        return AskOutcome(error="the sink returned no decision id")
    return AskOutcome(decision_id=decision_id, assignee=assignee)


def ask_event_data(*, question: str, why_only_you: str, done: str, options: Optional[List[str]], outcome: AskOutcome,
                   asker_user_id: str) -> Dict[str, Any]:
    """The ``data`` of the ``EVENT_DECISION`` event for a block (section 10). ``assigned_to_name`` is the consumer's to add."""
    assignee = outcome.assignee or Assignee(asker_user_id, "asker")
    return {"outcome": "asked", "question": question, "why_only_you": why_only_you, "done": done,
            "options": list(options or []), "assigned_to": assignee.user_id, "asked_of": assignee.asked_of,
            "assignee_fell_back": assignee.fell_back, "asker_user_id": asker_user_id}


def ask_text(done: str, question: str, assignee_name: str = "") -> str:
    """Plain text of an ask for a consumer that shows only text: what was done, then the one question. When the ask goes
    to a teammate, the line names them and says the answer comes back to the asker; otherwise it is the asker's own question."""
    head = str(done or "").strip()
    who = str(assignee_name or "").strip()
    line = (f"I have asked {who} for one thing so this can finish: {str(question).strip()} The answer will come back to you here."
            if who else f"I need one thing from you to finish this: {str(question).strip()}")
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


# ---- asks a worker raised itself in this process (the create_decision tool): the runner adopts them -------------------------------

def record_turn_ask(ledger: Any, *, decision_id: str, assignee_user_id: str, summary: str) -> None:
    """Note, in the turn's ledger (a list the orchestrator put in the tool context), that a decision-request exists for this turn."""
    if isinstance(ledger, list) and decision_id:
        ledger.append({"decision_id": str(decision_id), "assignee_user_id": str(assignee_user_id or ""), "summary": str(summary or "")})


def adopted_assignee(entry: Mapping[str, Any], participants: Sequence[Mapping[str, Any]], asker_user_id: str) -> Assignee:
    """Who a worker-raised ask is for, validated against the thread's participants: a participant other than the asker is a teammate;
    the asker, a stranger or nobody is the asker (the tool already filed it to the asker in that case, so nothing is claimed that is not so)."""
    who = str(entry.get("assignee_user_id") or "")
    people = {str(p.get("user_id") or "") for p in participants or [] if isinstance(p, Mapping)}
    if who and who != str(asker_user_id or "") and who in people:
        return Assignee(who, "member")
    return Assignee(str(asker_user_id or ""), "asker")


def ask_state_block(ledger: Any, participants: Sequence[Mapping[str, Any]], asker_user_id: str) -> str:
    """The structural fact the reply is written from: whether a decision-request exists for this turn and for whom. The reply may say
    someone was asked only when this says an ask exists."""
    entries = [e for e in (ledger or []) if isinstance(e, Mapping)]
    if not entries:
        return ("--- ASK STATE (INTERNAL, from the system's record) ---\nNo decision-request exists for this turn. Nobody has been asked, "
                "messaged or sent a request. Do not say or imply that anyone was.")
    assignee = adopted_assignee(entries[0], participants, asker_user_id)
    name = next((str(p.get("name") or "").strip() for p in participants or [] if isinstance(p, Mapping)
                 and str(p.get("user_id") or "") == assignee.user_id), "")
    target = name if assignee.asked_of == "member" and name else "the person who asked"
    return f"--- ASK STATE (INTERNAL, from the system's record) ---\nOne decision-request exists for this turn, addressed to {target}. No one else has been asked."
