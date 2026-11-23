"""The ``quest_improvements`` work source: keep every improvement idea people send to Quest moving, and tell them what became of it.

Quest AI is the rep that answers for the app itself across all users. This source is the autopilot half: before any model is asked, one cheap
read (``GET /api/assistant-tasks/suggestions/follow-up``) says what is unacted: ideas waiting for a decision, approved ideas not yet worked, and
resolved ideas whose suggester has not been told. The telling is deterministic (the backend mails the suggester as Quest AI with the team
groups on BCC, once each) and runs here with no model. Only what still needs judgment is listed as due; when nothing is, the run is refused
and closed as skipped, so a quiet day spends no tokens.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional, Sequence

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import log
from quest_ai_runner.intelligence.proactivity.autopilot_sources import WorkSource, register_source

PATH = "/api/assistant-tasks/suggestions/follow-up"
SOURCE_NAME = "quest_improvements"

SCOPE = ("keep every improvement idea sent through Improve Quest moving: no idea waits for a decision unnoticed, no approved idea stalls, "
         "and each suggester is told what became of their idea.")

RULES = """- The items listed below are the whole answer to what is unacted. Look at nothing else.
- An idea awaiting review: say in ONE add_quest_note line per idea whether to approve or reject it and why (user retention first), then raise ONE create_decision for the Spiritual Data reviewer with that recommendation. Never approve or reject it yourself.
- An approved idea not yet worked: check its task is queued or running in the maintainer lane. If it is stuck, say so in a note and raise a decision naming the blocker.
- Suggesters are told by the backend (Quest AI, team groups on BCC) once their idea is applied or not taken forward: never write to a user yourself.
- Post ONE short note on the quest at the end: how many ideas you looked at and what you asked a person to decide.
- Do nothing else on the quest."""


def fetch_state(client: Any) -> Optional[Mapping[str, Any]]:
    """The follow-up state, or None when the read failed (logged, never silent)."""
    try:
        return client.send_request("GET", PATH)
    except Exception as error:  # noqa: BLE001 -- a failed read must not abort the pass
        log.warning("quest_improvements: could not read the follow-up state: %s", error)
        return None


def tell_suggesters(client: Any) -> None:
    """Have the backend mail every resolved idea's suggester, once each. No model."""
    try:
        client.send_request("POST", PATH, body={})
    except Exception as error:  # noqa: BLE001
        log.warning("quest_improvements: telling suggesters failed: %s", error)


def improvements_due(client: Any) -> List[Mapping[str, Any]]:
    """Ideas needing judgment now (awaiting review, approved and not worked). Tells suggesters first when any are owed a mail."""
    state = fetch_state(client)
    if not state or not state.get("total"):
        return []
    if state.get("to_tell"):
        tell_suggesters(client)
    return [{"id": str(item.get("task_id")), "what": str(item.get("what") or item.get("title") or item.get("task_id"))}
            for item in state.get("items") or [] if item.get("kind") != "tell_suggester"]


def improvements_purpose(client: Any) -> WorkSource:
    def due_items(now: datetime) -> Sequence[Mapping[str, Any]]:
        return improvements_due(client)
    return WorkSource(SOURCE_NAME, SCOPE, RULES, due_items)


def register_for(client: Any) -> WorkSource:
    """Register the source bound to this lane's Quest client (the autopilot pass does this once at construction)."""
    return register_source(improvements_purpose(client))
