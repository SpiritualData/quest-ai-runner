"""Poller timing helpers: claim cooldown, quest-pass recurrence, series liveness and due-now checks.

Split out of ``poller.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from datetime import date, datetime, time, timedelta, timezone

import quest_ai_runner
from hubofhubs.api import WINDOW_DAYS, ordered_ids

from quest_ai_runner.the_intelligence.proactivity.autopilot import _parse_dt
from quest_ai_runner.the_intelligence.proactivity.quest_schedule import roster_days, run_pending
from quest_ai_runner.the_consciousness.request_intake.local_time import now_in_zone, today_in_zone


log = logging.getLogger("quest-ai-runner.poller")


# How long dispatch leaves a task alone after its claim fails (see ``_submit_to_pool``).
CLAIM_RETRY_COOLDOWN_SECONDS = 600.0
MINUTES_PER_DAY = 1440


def _quest_pass_days(autopilot_cfg: Dict[str, Any]) -> Optional[List[str]]:
    """The weekdays this quest's own pass series should ever fire on (``Mon``..``Sun``), or ``None`` for every day.

    The roster's days are the hub's ``window`` days (``quest_schedule.roster_days``), the same days the gate asks the hub's Proactivity about, so the series
    never fires on a day the gate would skip: no "nothing to do" occurrence is created only to report a skip (see ``_create_quest_pass``).
    """
    days = roster_days(autopilot_cfg)
    return [day.title() for day in days] if days else None


def _next_allowed_date(start: date, allowed_days: List[str]) -> date:
    """The first date on/after ``start`` whose weekday is in ``allowed_days`` (``Mon``..``Sun``, matching Python's Monday=0..Sunday=6 ``date.weekday()``)."""
    allowed_idx = {WINDOW_DAYS.index(d.lower()[:3]) for d in allowed_days if d.lower()[:3] in WINDOW_DAYS}
    for offset in range(7):
        candidate = start + timedelta(days=offset)
        if candidate.weekday() in allowed_idx:
            return candidate
    return start  # unreachable: allowed_days is never empty when this is called


def _quest_pass_recurrence(entry: Dict[str, Any], expected_time: str) -> Dict[str, Any]:
    """The ``recurrence`` a quest's own pass series should carry: ``weekly`` restricted to the
    roster's on-duty days when ``_quest_pass_days`` finds a restriction, else the unrestricted
    ``daily`` every quest had before the day rule existed. This is the one place that decides the
    SHAPE of the series, used both to create it and to retune it, so the two can never disagree.
    """
    allowed_days = _quest_pass_days(entry)
    if allowed_days:
        return {"frequency": "weekly", "days": allowed_days, "time": expected_time}
    return {"frequency": "daily", "time": expected_time}


# A generic multi-day window, comfortably wider than a daily cadence so a live weekly (or
# monthly) foreign series is never misread as dead just because nothing has touched it in the
# last day or two -- see ``_foreign_series_looks_alive``.
_FOREIGN_SERIES_STALE_AFTER_DAYS = 3


def _foreign_series_looks_alive(occ: Dict[str, Any], entry: Dict[str, Any],
                                now: datetime) -> bool:
    """Whether a foreign-owned SERIES occurrence still looks like a LIVE pass (True) rather than a
    genuinely stale/abandoned one (False).

    This is the test ``_ensure_one_quest_pass`` uses to decide whether creating our own competing
    series would duplicate a series another account's lane is actually servicing, or would be the
    only thing standing between this quest and never getting an autopilot pass again. Neither
    extreme is safe as a blanket rule: always ignoring a foreign series (the old behaviour)
    resurrects the ORIGINAL bug this method's docstring already names -- a quest whose foreign
    pass is permanently dead (created before passes carried ``assignee_user_id``, or that
    account's lane is gone for good) never gets a pass again, silently, until a human clears it in
    the app. Never creating our own when ANY foreign series exists brings back the duplicate-series
    bug this heuristic exists to fix -- two lanes each maintaining their own series for the same
    quest.

    Primary signal: ``scheduled_date``, read in the quest's own zone (matching
    ``_expected_quest_occurrence``'s date arithmetic). A series occurrence dated today or later
    means SOME lane is advancing it on schedule -- alive. One dated in the past means nothing has
    advanced it past its own due date, which is exactly what an abandoned lane looks like.

    Fallback when ``scheduled_date`` is missing or unparseable: ``updated_at`` recency (via
    ``autopilot._parse_dt``, the one existing way this repo parses a stored ISO timestamp) against
    ``_FOREIGN_SERIES_STALE_AFTER_DAYS``.

    Both fields missing/unparseable default to ALIVE -- the conservative choice. Being cautious
    about NOT creating a duplicate costs nothing here: the loud per-scan foreign-occurrence warning
    already logged by the caller is what tells a human to go clear a genuinely dead one; this
    heuristic only decides whether THIS lane also creates a second series in the meantime.
    """
    zone = entry.get("run_timezone")
    scheduled = str(occ.get("scheduled_date") or "").strip()
    if scheduled:
        try:
            scheduled_date = date.fromisoformat(scheduled)
        except ValueError:
            scheduled_date = None
        if scheduled_date is not None:
            return scheduled_date >= today_in_zone(zone, now)
    updated = _parse_dt(occ.get("updated_at"))
    if updated is None:
        return True
    age = now_in_zone(zone, now) - now_in_zone(zone, updated)
    return age.days < _FOREIGN_SERIES_STALE_AFTER_DAYS


def _run_request_signature(quest_id: str, entry: Dict[str, Any]) -> str:
    """The dedup key for ONE pending "Run now" on one quest, or ``""`` when none is pending.

    Keyed on the request's own ``run_requested_at`` instant, so pressing the button again is a
    different request and gets its own pass, while the SAME press can only ever be served once.
    Stored in the lane's ordinary ``StateStore``, the same place a task's signature lives, so it
    survives a restart and needs no new storage.
    """
    if not quest_id or not run_pending(entry):
        return ""
    return f"autopilot-run-request:{quest_id}:{entry.get('run_requested_at')}"


# What a task is to the dispatcher, first first: a task a person pressed "Start now" on, then a task a person is waiting on, then any other.
DISPATCH_KINDS = ("start-requested", "real-time", "task")


def dispatch_order(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The tasks in the order the hub's Prioritization gives them (Quest's ordering arm), with the kinds of ``DISPATCH_KINDS`` as the order of kinds and the
    oldest first within a kind: priority, not a separate lane."""
    if len(tasks) < 2:
        return list(tasks)
    candidates = [{"id": str(index), "kind": ("start-requested" if task.get("start_requested_at") else "real-time" if task.get("real_time") else "task"),
                   "asked_at": str(task.get("created_at") or "")} for index, task in enumerate(tasks)]
    answer = quest_ai_runner.run("prioritization", {"candidates": candidates, "subject": "work", "stated": {"kind_order": list(DISPATCH_KINDS)}, "now": datetime.now(timezone.utc).isoformat()})
    return [tasks[int(place)] for place in ordered_ids(answer["ordered"])]


def _task_signature(task: Dict[str, Any]) -> str:
    """A stable per-task signature so each task fires exactly once.

    Includes status + an updated/scheduled marker so a re-queued or rescheduled task is treated
    as new, but the same queued task seen twice is deduped.
    """
    tid = task.get("id") or task.get("task_id") or ""
    marker = task.get("updated_at") or task.get("scheduled_time") or task.get("scheduled_date") or ""
    return f"{tid}:{task.get('status', 'queued')}:{marker}"


def tasks_due_now(tasks: List[Dict[str, Any]],
                  now: Optional[datetime] = None,
                  tz_for: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                  ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split discovered tasks into (due, not-yet-due) by LOCAL wall clock.

    Discovery asks the backend for `due_before=<ISO now, UTC>`, but the backend compares only the
    DATE portion of that timestamp against `scheduled_date` and never looks at `scheduled_time`
    (see quest-backend `assistant_task_storage.list_tasks`). Its answer is therefore a SUPERSET:
    correct to the day, silent about the hour. West of UTC that superset opens early: a task set
    for 06:30 becomes "due" the moment UTC midnight passes, which is 17:00 the PREVIOUS afternoon
    in US/Pacific (seen 2026-08-12/13 on the personal lane: a 06:30 brief ran at 17:17 the day
    before, twice).

    So narrow the superset here, where the runner knows the wall clock the schedule was written
    against. The time of day is the hub's Proactivity (``quest_ai_runner.run("proactivity")``):
    a task dated today is a schedule whose ``window`` is that weekday, its ``scheduled_time`` and
    the task's zone, open for the rest of the day. What stays here is the Quest-shaped part: a task
    with no date, or a ``start_at`` instant the backend already held it until, is due; a task dated
    before today is due (it stays queued, so holding one back would strand it); a task dated after
    today is not yet due; an unreadable schedule is treated as due, never stranded. A missing time
    means midnight.

    The zone is `tz_for(task)` (a per-quest autopilot pass's own `run_timezone`); a task it returns
    nothing for, or an unresolvable zone, is read on the runner's own clock, never UTC.
    """
    verdict: Dict[int, bool] = {}
    todays: Dict[str, List[tuple]] = {}
    for index, task in enumerate(tasks):
        if str(task.get("start_at") or "").strip():
            verdict[index] = True
            continue
        day_text = str(task.get("scheduled_date") or "").strip()
        if not day_text:
            verdict[index] = True
            continue
        clock = str(task.get("scheduled_time") or "00:00").strip()[:5]
        try:
            day = date.fromisoformat(day_text)
            time.fromisoformat(clock)
        except ValueError:
            log.warning("task %s has an unreadable schedule (%r %r), treating it as due",
                        task.get("task_id") or task.get("id"), day_text, clock)
            verdict[index] = True
            continue
        zone = (tz_for(task) if tz_for else None) or ""
        today = today_in_zone(zone, now)
        if day != today:
            verdict[index] = day < today
            continue
        instant = now_in_zone(zone, now)
        if instant.tzinfo is None:
            instant = instant.astimezone()
        schedule = {"name": str(index), "request": str(task.get("task_id") or task.get("id") or index),
                    "window": {"days": [WINDOW_DAYS[day.weekday()]], "run_time": clock, "time_zone": zone, "grace_minutes": MINUTES_PER_DAY},
                    "last_run": ""}
        todays.setdefault(instant.isoformat(), []).append((index, schedule))
    for moment, entries in todays.items():
        answer = quest_ai_runner.run("proactivity", {"schedules": [schedule for _, schedule in entries], "now": moment})
        open_now = {int(trigger["schedule"]) for trigger in answer}
        verdict.update({index: index in open_now for index, _ in entries})
    due = [task for index, task in enumerate(tasks) if verdict[index]]
    deferred = [task for index, task in enumerate(tasks) if not verdict[index]]
    return due, deferred
