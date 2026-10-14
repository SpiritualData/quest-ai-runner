"""Poller timing helpers: claim cooldown, quest-pass recurrence, series liveness and due-now checks.

Split out of ``poller.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from datetime import date, datetime, timedelta

from .autopilot import _parse_dt, persona_entries_on_duty, run_requested
from .local_time import now_in_zone, scheduled_moment, today_in_zone


log = logging.getLogger("quest-ai-runner.poller")


# How long dispatch leaves a task alone after its claim fails (see ``_submit_to_pool``).
CLAIM_RETRY_COOLDOWN_SECONDS = 600.0


_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _quest_pass_days(autopilot_cfg: Dict[str, Any], reference: date) -> Optional[List[str]]:
    """The weekdays this quest's own pass series should ever fire on, or ``None`` for every day.

    Mirrors ``AutopilotPass._gate_quest``'s day rule exactly, by calling the exact predicate the
    gate itself uses (``persona_entries_on_duty``) once per weekday instead of restating its
    logic: a roster with no entries, or with any entry that is on duty on every probed day (no
    ``days`` restriction), passes the gate every day, so the series stays daily. Only when the
    roster excludes at least one day is there anything to restrict -- scheduling the series to
    fire only on the days it could possibly do something is what keeps a "nothing to do"
    occurrence from ever being created, instead of being created and then reporting a skip (see
    ``_create_quest_pass``).

    ``reference`` only anchors the 7-day probe to a real calendar (for correct DST/weekday
    arithmetic); any date works since a full week is walked either way.
    """
    if not (autopilot_cfg.get("personas") or []):
        return None
    on_duty: List[str] = []
    for offset in range(7):
        probe = reference + timedelta(days=offset)
        if persona_entries_on_duty(autopilot_cfg, probe):
            on_duty.append(probe.strftime("%a"))
    if len(on_duty) >= 7:
        return None  # on duty every day -- no restriction to apply
    days = set(on_duty)
    return [d for d in _DAY_ORDER if d in days] or None


def _next_allowed_date(start: date, allowed_days: List[str]) -> date:
    """The first date on/after ``start`` whose weekday is in ``allowed_days`` (``_DAY_ORDER``
    abbreviations, matching Python's Monday=0..Sunday=6 ``date.weekday()``)."""
    allowed_idx = {_DAY_ORDER.index(d) for d in allowed_days if d in _DAY_ORDER}
    for offset in range(7):
        candidate = start + timedelta(days=offset)
        if candidate.weekday() in allowed_idx:
            return candidate
    return start  # unreachable: allowed_days is never empty when this is called


def _quest_pass_recurrence(entry: Dict[str, Any], expected_time: str,
                           reference: date) -> Dict[str, Any]:
    """The ``recurrence`` a quest's own pass series should carry: ``weekly`` restricted to the
    roster's on-duty days when ``_quest_pass_days`` finds a restriction, else the unrestricted
    ``daily`` every quest had before the day rule existed. This is the one place that decides the
    SHAPE of the series, used both to create it and to retune it, so the two can never disagree.
    """
    allowed_days = _quest_pass_days(entry, reference)
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
    if not quest_id or not run_requested(entry):
        return ""
    return f"autopilot-run-request:{quest_id}:{entry.get('run_requested_at')}"


def _task_signature(task: Dict[str, Any]) -> str:
    """A stable per-task signature so each task fires exactly once.

    Includes status + an updated/scheduled marker so a re-queued or rescheduled task is treated
    as new, but the same queued task seen twice is deduped.
    """
    tid = task.get("id") or task.get("task_id") or ""
    marker = task.get("updated_at") or task.get("scheduled_time") or task.get("scheduled_date") or ""
    return f"{tid}:{task.get('status', 'queued')}:{marker}"


def _due_now_locally(tasks: List[Dict[str, Any]],
                     now: Optional[datetime] = None,
                     tz_for: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                     ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split discovered tasks into (due, not-yet-due) by LOCAL wall clock.

    Discovery asks the backend for `due_before=<ISO now, UTC>`, but the backend compares only the
    DATE portion of that timestamp against `scheduled_date` and never looks at `scheduled_time`
    (see quest-backend `assistant_task_storage.list_tasks`). Its answer is therefore a SUPERSET:
    correct to the day, silent about the hour. West of UTC that superset opens early -- a task set
    for 06:30 becomes "due" the moment UTC midnight passes, which is 17:00 the PREVIOUS afternoon
    in US/Pacific. A daily morning brief then runs the evening before, is written against the wrong
    day, and burns the occurrence its real slot needed. (Seen 2026-08-12/13 on the personal lane:
    a 06:30 brief ran at 17:17 the day before, twice.)

    So narrow the superset here, where the runner knows the wall clock the schedule was written
    against. A task is due once local `scheduled_date` + `scheduled_time` has arrived; a missing
    time means midnight, and an unscheduled task ("do it now", the chat-delegated case) is always
    due. Holding one back is lossless: it stays `queued` and surfaces on a later scan.

    Timezone, for a task `tz_for` returns nothing for: the runner's own local time, which is the
    tz a person authoring "06:30" means when no zone was ever stated. Byte-for-byte the original
    behaviour -- `tz_for` left `None`, or returning `None` for a given task, changes nothing here.

    `tz_for`, when given, maps a task to an IANA zone name (a per-quest autopilot pass's own
    `run_timezone` -- see `Poller._pass_timezone_for`). For a task it resolves one for, the
    comparison happens in THAT zone instead (`local_time.scheduled_moment`/`now_in_zone`), which
    is what fixes the same early-firing bug for a quest whose owner is not in the runner host's
    own timezone. An unresolvable zone name degrades to the same naive local comparison every
    other task gets (never raises, logs once via `local_time.resolve_zone`).
    """
    legacy_now = now or datetime.now()
    due: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    for task in tasks:
        # A task carrying an explicit ``start_at`` (a UTC instant) was already held by the backend
        # until that instant: discovery only returns it once it has arrived. It is due, whatever
        # this runner's own clock makes of its local-date schedule.
        if str(task.get("start_at") or "").strip():
            due.append(task)
            continue
        date = str(task.get("scheduled_date") or "").strip()
        if not date:
            due.append(task)
            continue
        clock = str(task.get("scheduled_time") or "00:00").strip()[:5]
        zone_name = tz_for(task) if tz_for else None
        if zone_name:
            scheduled = scheduled_moment(date, clock, zone_name)
            if scheduled is not None and scheduled.tzinfo is not None:
                moment = now_in_zone(zone_name, now)
                (due if scheduled <= moment else deferred).append(task)
                continue
            # The zone itself did not resolve (scheduled_moment fell back to naive, or failed to
            # parse at all) -- fall through to the same legacy comparison every other task gets.
        try:
            scheduled = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
        except ValueError:
            # An unparseable schedule must never strand a task: fall back to the backend's answer.
            log.warning("task %s has an unreadable schedule (%r %r) — treating it as due",
                        task.get("task_id") or task.get("id"), date, clock)
            due.append(task)
            continue
        (due if scheduled <= legacy_now else deferred).append(task)
    return due, deferred
