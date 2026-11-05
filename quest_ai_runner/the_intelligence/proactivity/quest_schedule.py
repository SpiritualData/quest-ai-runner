"""A quest's autopilot settings as the hub's schedules, and the hub's Proactivity asked what is due.

WHEN a pass is due is not decided here: it is the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``): the calendar period in the schedule's own
zone, a run someone requested after the last run, an event after the last run for a reactive schedule. This module only says what a quest's settings mean as
schedules (``autopilot.cadence``, ``last_pass_at``, ``run_requested_at``, ``run_timezone``, pending work that makes a quest due daily) and what its asks
mean as events (``quest_events.QuestEvents``, an adapter of Intake Sources).
"""
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

import quest_ai_runner
from hubofhubs.api import attach_events

from quest_ai_runner.the_body.intake_sources.quest_events import QuestEvents, SOURCE

CADENCE = "cadence"
REACTIVE = "reactive"
REQUESTED = "requested"


def due(schedules: Sequence[Mapping[str, Any]], now: Optional[datetime]) -> Sequence[Mapping[str, Any]]:
    return quest_ai_runner.run("proactivity", {"schedules": list(schedules), "now": now.isoformat() if now else ""})


def due_trigger(autopilot_cfg: Mapping[str, Any], now: Optional[datetime] = None, zone: Optional[str] = None, pending: int = 0,
                honour_run_request: bool = True) -> str:
    """The kind of trigger that makes the quest's pass due now (``requested``: a "Run now" not yet answered, or ``period``: a new calendar period in the
    quest's zone), or ``""`` when it is not due. Pending work makes the quest due daily at least. ``honour_run_request=False`` leaves the request out."""
    schedule = {"name": CADENCE, "request": "autopilot pass", "cadence": "daily" if pending > 0 else str(autopilot_cfg.get("cadence") or "weekly").strip().lower(),
                "zone": zone or "", "last_run": autopilot_cfg.get("last_pass_at") or ""}
    if honour_run_request:
        schedule["run_requested_at"] = autopilot_cfg.get("run_requested_at") or ""
    found = due([schedule], now)
    return str(found[0]["kind"]) if found else ""


def run_pending(autopilot_cfg: Mapping[str, Any]) -> bool:
    """Whether a "Run now" request is pending: asked after the last pass, so a finished pass spends it and nothing has to clear it."""
    return due_trigger(autopilot_cfg) == REQUESTED


def new_ask_since(asks: Sequence[Mapping[str, Any]], last_pass_at: Optional[str], now: Optional[datetime] = None) -> bool:
    """Whether any of the quest's ``asks`` landed after its last pass (every ask counts when it has never passed): the event a reactive quest waits for."""
    schedule = {"name": REACTIVE, "request": "autopilot pass", "event_source": SOURCE, "last_run": last_pass_at or ""}
    events = QuestEvents(asks).pull(SOURCE, limit=len(asks))
    return bool(due(attach_events([schedule], events), now))
