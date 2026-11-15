"""Poller timing helpers: claim cooldown, dispatch order and due-now checks.

Split out of ``poller.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from datetime import date, datetime, time, timedelta, timezone

import quest_ai_runner
from hubofhubs.api import WINDOW_DAYS, ordered_ids

from quest_ai_runner.consciousness.request_intake.local_time import now_in_zone, today_in_zone


log = logging.getLogger("quest-ai-runner.poller")


# How long dispatch leaves a task alone after its claim fails (see ``_submit_to_pool``).
CLAIM_RETRY_COOLDOWN_SECONDS = 600.0
MINUTES_PER_DAY = 1440


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
