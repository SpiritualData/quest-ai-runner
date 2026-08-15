"""A scheduled task fires at its LOCAL clock time, not at UTC midnight.

The backend's discovery filter compares only the DATE portion of ``due_before`` against
``scheduled_date`` and ignores ``scheduled_time`` entirely, so its answer is a superset: right to
the day, silent about the hour. Anywhere west of UTC that superset opens early. In US/Pacific a
06:30 task goes "due" at 17:00 the PREVIOUS afternoon, which is how a daily 06:30 dissertation
brief ran at 17:17 the day before, twice (2026-08-12 and 08-13), each time writing itself against
the wrong day and consuming the occurrence its real morning slot needed.

``_due_now_locally`` narrows that superset in the runner, where the wall clock the schedule was
written against is actually known. These tests pin both halves: nothing fires early, and nothing
gets stranded.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from quest_ai_runner.runner.poller import _due_now_locally

from .test_context_request_fast_lane import _poller_with_assembler
from .test_runner import MockQuestClient


def _at(when: datetime):
    """A task scheduled at `when`, in the naive local form the backend stores."""
    return {"scheduled_date": when.strftime("%Y-%m-%d"), "scheduled_time": when.strftime("%H:%M")}


# --- the filter itself -----------------------------------------------------------

def test_morning_task_is_held_until_its_local_time():
    """THE regression: tomorrow 06:30 must not fire tonight, however UTC has rolled."""
    now = datetime(2026, 8, 13, 17, 3)          # 17:03 local == 00:03 UTC the next day
    tomorrow_brief = {"task_id": "brief", **_at(datetime(2026, 8, 14, 6, 30))}

    due, deferred = _due_now_locally([tomorrow_brief], now=now)

    assert due == []
    assert deferred == [tomorrow_brief]


def test_task_fires_once_its_local_time_arrives():
    now = datetime(2026, 8, 14, 6, 30)
    brief = {"task_id": "brief", **_at(datetime(2026, 8, 14, 6, 30))}

    due, deferred = _due_now_locally([brief], now=now)

    assert due == [brief]
    assert deferred == []


def test_unscheduled_task_is_always_due():
    """Chat-delegated work carries no schedule and must never be held back."""
    now = datetime(2026, 8, 13, 17, 3)
    task = {"task_id": "delegated", "scheduled_date": None, "scheduled_time": None}

    due, _ = _due_now_locally([task], now=now)

    assert due == [task]


def test_missing_time_means_midnight_so_an_older_task_still_runs():
    now = datetime(2026, 8, 13, 17, 3)
    dated_only = {"task_id": "dated", "scheduled_date": "2026-08-13", "scheduled_time": None}
    yesterday = {"task_id": "old", "scheduled_date": "2026-08-12", "scheduled_time": None}

    due, deferred = _due_now_locally([dated_only, yesterday], now=now)

    assert due == [dated_only, yesterday]
    assert deferred == []


def test_unreadable_schedule_is_treated_as_due_not_stranded(caplog):
    """Falling back to the backend's answer beats silently never running the task."""
    now = datetime(2026, 8, 13, 17, 3)
    garbled = {"task_id": "garbled", "scheduled_date": "next tuesday", "scheduled_time": "??"}

    due, deferred = _due_now_locally([garbled], now=now)

    assert due == [garbled]
    assert deferred == []
    assert "unreadable schedule" in caplog.text


def test_mixed_batch_splits_on_the_clock_not_the_date():
    """Two tasks on the SAME date, one past and one future: the date alone cannot separate them."""
    now = datetime(2026, 8, 14, 12, 0)
    morning = {"task_id": "morning", **_at(datetime(2026, 8, 14, 6, 30))}
    evening = {"task_id": "evening", **_at(datetime(2026, 8, 14, 21, 0))}

    due, deferred = _due_now_locally([morning, evening], now=now)

    assert due == [morning]
    assert deferred == [evening]


# --- through the poller ----------------------------------------------------------

def test_run_once_does_not_claim_a_task_whose_local_time_has_not_arrived():
    """End to end: the backend hands back tomorrow's brief, the scan leaves it queued."""
    tomorrow = datetime.now() + timedelta(days=1)
    client = MockQuestClient([{
        "id": "brief-1", "status": "queued", "team_id": "team1",
        "scheduled_date": tomorrow.strftime("%Y-%m-%d"), "scheduled_time": "06:30",
        "context_request": {"query": "today's brief", "max_chars": None},
    }])
    poller, _ = _poller_with_assembler(client)

    assert poller.run_once() == []
    assert client.claimed == []
    assert client.reports == []


def test_run_once_still_claims_a_task_that_is_due():
    """The other half: the filter must not swallow real work (a defer-everything bug would pass
    the test above on its own)."""
    earlier_today = datetime.now().replace(hour=0, minute=0)
    client = MockQuestClient([{
        "id": "ctx-1", "status": "queued", "team_id": "team1",
        "scheduled_date": earlier_today.strftime("%Y-%m-%d"), "scheduled_time": "00:00",
        "context_request": {"query": "what happened wednesday", "max_chars": None},
    }])
    poller, _ = _poller_with_assembler(client)

    assert poller.run_once() == ["ctx-1"]
    assert client.claimed == ["ctx-1"]
