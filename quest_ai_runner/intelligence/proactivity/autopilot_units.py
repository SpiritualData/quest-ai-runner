"""Named autopilots as the runner reads them: which ones run, who is on duty today, and what a run is called.

A quest holds any number of NAMED autopilots (``autopilot.autopilots``, each storing every setting it uses; nothing is inherited). The backend decides
WHEN each runs (its scheduler asks the hub's Proactivity and queues one ``autopilot_run`` task per run, carrying ``autopilot_id``); the runner only
executes that run. This module is the small vocabulary the executor needs to read an autopilot: the run rule and the duty rule.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from hubofhubs.api import WINDOW_DAYS

from quest_ai_runner.consciousness.request_intake.local_time import now_in_zone

AUTOPILOT_RUN_KIND = "autopilot_run"


def entries_of(autopilot_cfg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The quest's autopilots in list order (an entry with no id is dropped)."""
    return [dict(e) for e in (autopilot_cfg.get("autopilots") or []) if isinstance(e, Mapping) and str(e.get("id") or "").strip()]


def runs(entry: Mapping[str, Any], autopilot_cfg: Mapping[str, Any]) -> bool:
    """The run rule: the backend's own answer when it serves one (``status.runs``), else the autopilot is enabled, not paused by the plan's cap, and
    the quest is not paused."""
    served = entry.get("status")
    if isinstance(served, Mapping) and "runs" in served:
        return bool(served["runs"])
    return entry.get("enabled") is not False and not entry.get("paused_by_plan") and not autopilot_cfg.get("paused")


def units_of(autopilot_cfg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The autopilots that run, in list order."""
    return [e for e in entries_of(autopilot_cfg) if runs(e, autopilot_cfg)]


def unit_named(autopilot_cfg: Mapping[str, Any], autopilot_id: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries_of(autopilot_cfg) if e["id"] == autopilot_id), None)


def on_duty(entry: Mapping[str, Any], now: datetime, zone: Optional[str]) -> bool:
    """Whether the autopilot works today in the quest's zone: it names no days, or names today's. Its time of day is the scheduler's, not a duty."""
    days = {str(d).strip().lower()[:3] for d in (entry.get("days") or [])}
    return not days or WINDOW_DAYS[now_in_zone(zone, now).weekday()] in days


def label_of(entry: Mapping[str, Any]) -> str:
    return str(entry.get("name") or entry.get("id") or "").strip()


def scope_of(entry: Mapping[str, Any]) -> str:
    """What the autopilot is for, as a goal is judged against it: its purpose sentence and its prompt."""
    return f"{str(entry.get('purpose') or '').strip()}\n{str(entry.get('instructions') or '').strip()}".strip()
