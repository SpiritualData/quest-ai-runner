"""A decision-request an out-of-process worker filed itself becomes the turn's ask, filed through the runner's own path.

A deep worker with its own credentials can call the decisions endpoint directly. Its row has no turn dedupe key, an assignee nobody
checked and nobody told, and the backend has no reassign route. After the attempt the runner finds the rows that appeared on the quest
(the before and after snapshot of ``quest_open_decision_ids``), files ONE ask through ``ask_policy.file_ask`` (same dedupe key, assignee
validated against the participants, asker notified) and then withdraws the worker's rows. Re-filing comes first, so a failure leaves the
worker's row standing for the person rather than losing the ask. Structural only: row fields and ids, never wording.

The sink capabilities are optional (``open_decisions_for_quest``, ``withdraw_decision``); a sink without them adopts nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import ask_dedupe_key, adopted_assignee, file_ask, record_turn_ask, why_only_you_of

log = logging.getLogger(__name__)


def row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("decision_id") or row.get("id") or "")


def adopt_worker_asks(sink: Any, *, quest_id: Optional[str], before: FrozenSet[str], meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-file the rows that appeared on ``quest_id`` since ``before`` as the turn's one ask; the ledger ``meta['turn_asks']`` records it.

    Returns the ledger entries added (empty when nothing was adopted). Never raises."""
    lister, withdraw = getattr(sink, "open_decisions_for_quest", None), getattr(sink, "withdraw_decision", None)
    if not quest_id or not callable(lister) or not callable(withdraw):
        return []
    context = meta or {}
    turn_key = ask_dedupe_key(str(context.get("turn_id") or ""), str(context.get("conv_id") or ""))
    try:
        fresh = [r for r in (lister(quest_id) or []) if isinstance(r, Mapping) and row_id(r) and row_id(r) not in before
                 and str(r.get("dedupe_key") or "") != turn_key]
    except Exception:  # noqa: BLE001 -- a failed probe must never break the run
        return []
    if not fresh:
        return []
    asker = str(context.get("asker_user_id") or context.get("requester_user_id") or context.get("user_id") or "")
    participants = context.get("participants") or []
    added: List[Dict[str, Any]] = []
    for row in fresh:
        who = str(row.get("assigned_to") or row.get("assigned_to_user_id") or "")
        assignee = adopted_assignee({"assignee_user_id": who}, participants, asker)
        summary = str(row.get("summary") or "")
        outcome = file_ask(sink, question=summary, why_only_you=why_only_you_of("needs_person", assignee, participants), done="", options=None,
                           quest_id=quest_id, conv_id=str(context.get("conv_id") or ""), turn_id=str(context.get("turn_id") or ""),
                           asker_user_id=asker, assignee=assignee, summary=summary)
        if not outcome.filed:
            log.warning("worker ask %s could not be re-filed (%s); left standing", row_id(row), outcome.error)
            continue
        if outcome.decision_id != row_id(row):
            try:
                withdraw(row_id(row))
            except Exception as e:  # noqa: BLE001 -- a duplicate row is the lesser harm
                log.warning("worker ask %s was re-filed as %s but could not be withdrawn: %s", row_id(row), outcome.decision_id, e)
        if not any(e.get("decision_id") == outcome.decision_id for e in added):
            entry = {"decision_id": outcome.decision_id, "assignee_user_id": assignee.user_id, "summary": summary}
            added.append(entry)
            record_turn_ask(context.get("turn_asks"), **entry)
    return added
