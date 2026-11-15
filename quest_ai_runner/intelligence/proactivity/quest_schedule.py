"""A quest's autopilot settings as the hub's schedules, and the hub's Proactivity asked what is due.

WHEN a pass is due is not decided here: it is the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``): the calendar period in the schedule's own
zone, a window or recurrence of days and a time of day, a run someone requested after the last run, an event after the last run. The backend's scheduler asks
it for each autopilot; this module only asks the hub (``due``) and says what a quest's asks mean as events (``quest_events.QuestEvents``, an adapter of
Intake Sources).
"""
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

import quest_ai_runner
from hubofhubs.api import attach_events

from quest_ai_runner.body.intake_sources.quest_events import QuestEvents, SOURCE

REACTIVE = "reactive"
REQUESTED = "requested"


def due(schedules: Sequence[Mapping[str, Any]], now: Optional[datetime]) -> Sequence[Mapping[str, Any]]:
    return quest_ai_runner.run("proactivity", {"schedules": list(schedules), "now": now.isoformat() if now else ""})


def answer_rows(asks: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Each note a PERSON wrote on an ask, as a row dated when they wrote it. An answer to a question the AI
    raised (or a reply under an ask it is working) adds history to an existing ask, not a new ask row, so
    without these rows a reactive quest would never run the pass that acts on the answer."""
    rows: List[Dict[str, Any]] = []
    for ask in asks:
        for entry in ask.get("history") or []:
            if isinstance(entry, Mapping) and entry.get("by") == "person" and str(entry.get("note") or "").strip():
                rows.append({"text": str(entry.get("note")), "author": str(ask.get("author") or ""),
                             "occurred_at": entry.get("at") or ask.get("occurred_at")})
    return rows


def new_ask_since(asks: Sequence[Mapping[str, Any]], last_pass_at: Optional[str], now: Optional[datetime] = None) -> bool:
    """Whether any of the quest's ``asks`` landed after its last pass, or a person answered or replied on one after it
    (every ask counts when it has never passed): the event a reactive quest waits for."""
    schedule = {"name": REACTIVE, "request": "autopilot pass", "event_source": SOURCE, "last_run": last_pass_at or ""}
    rows = [dict(ask) for ask in asks] + answer_rows(asks)
    events = QuestEvents(rows).pull(SOURCE, limit=len(rows))
    return bool(due(attach_events([schedule], events), now))
