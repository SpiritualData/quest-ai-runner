"""A quest's autopilot settings as the hub's schedules, and the hub's Proactivity asked what is due.

WHEN a pass is due is not decided here: it is the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``): the calendar period in the schedule's own
zone, a run someone requested after the last run, an event after the last run for a reactive schedule. This module only says what a quest's settings mean as
schedules (``autopilot.cadence``, ``last_pass_at``, ``run_requested_at``, ``run_timezone``, pending work that makes a quest due daily) and what its asks
mean as events (``quest_events.QuestEvents``, an adapter of Intake Sources).
"""
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence, Tuple

import quest_ai_runner
from hubofhubs.api import WINDOW_DAYS, Window, attach_events

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


def roster_days(autopilot_cfg: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    """The days of the week (the hub's ``WINDOW_DAYS`` names) on which a quest's roster has someone on duty, or ``None`` when it does not restrict the days: no
    roster, or an entry with no ``days`` (on duty every day). A rostered character who works only some days narrows the quest to the union of those days."""
    personas = autopilot_cfg.get("personas") or []
    if not personas or any(not persona.get("days") for persona in personas):
        return None
    kept = {str(day).strip().lower()[:3] for persona in personas for day in persona["days"]}
    days = tuple(day for day in WINDOW_DAYS if day in kept)
    return days if days and len(days) < len(WINDOW_DAYS) else None


def roster_day_open(autopilot_cfg: Mapping[str, Any], now: datetime) -> bool:
    """Whether today, in the quest's own zone, is a day its roster keeps: the hub's Proactivity asked about a ``window`` of the roster's days (open from midnight
    for the whole day), so the day rule is the hub's and no weekday arithmetic is repeated here."""
    days = roster_days(autopilot_cfg)
    if days is None:
        return True
    window = {"days": list(days), "run_time": "00:00", "time_zone": str(autopilot_cfg.get("run_timezone") or ""), "grace_minutes": 1440}
    return bool(due([{"name": "roster", "request": "roster day", "window": window, "last_run": ""}], now))
