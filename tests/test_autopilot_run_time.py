"""Per-quest ``run_time``: one pass series per opted-in quest, at its own hour.

There is no second kind of pass. A quest that names no ``run_time`` is given the lane's default
hour when its schedule is read, so it gets its own series like every other one; any surviving
team-wide series is retired. These tests pin that, the schedule-correction sweep
(A3: the backend spawns the next occurrence on a UTC date, which is wrong for a run time west
enough to cross UTC midnight), the catch-up formula, retirement, and duplicate handling -- all of
it against ``FakeRunTimeClient``, a thin extension of ``test_autopilot_pass_task``'s
``FakePassClient`` that adds ``update_task`` and a 409 knob.
"""
from datetime import datetime, timezone

import pytest

from quest_ai_runner.runner.autopilot import AUTOPILOT_PASS_KIND, OPEN_TASK_STATUSES
from quest_ai_runner.runner.quest_client import QuestApiError

from .test_autopilot_pass_task import FakePassClient, _poller

NOW = datetime(2026, 8, 20, 9, 0, 0, tzinfo=timezone.utc)  # a Thursday


class FakeRunTimeClient(FakePassClient):
    """Adds ``update_task`` (retune/retire) and an optional error to raise from it, on top of
    ``FakePassClient``'s create/list/state-read behaviour."""

    def __init__(self, *, update_error=None, **kwargs):
        super().__init__(**kwargs)
        self.update_error = update_error
        self.task_updates = []  # (task_id, fields)

    def update_task(self, task_id, fields):
        if self.update_error:
            raise self.update_error
        self.task_updates.append((task_id, dict(fields)))
        for t in self.tasks:
            if t.get("id") == task_id:
                t.update(fields)
        return {"id": task_id, **fields}


def _pass_task(task_id, *, quest_id, status="queued", scheduled_date=None, scheduled_time=None,
              recurrence_time=None):
    t = {"id": task_id, "task_kind": AUTOPILOT_PASS_KIND, "status": status, "goal_id": quest_id}
    if scheduled_date is not None:
        t["scheduled_date"] = scheduled_date
    if scheduled_time is not None:
        t["scheduled_time"] = scheduled_time
    if recurrence_time is not None:
        t["recurrence"] = {"frequency": "daily", "time": recurrence_time}
    return t


def _poller_now(client, now, **cfg_overrides):
    p = _poller(client, **cfg_overrides)
    p._now = lambda: now
    return p


# --- 1: a quest with run_time gets its own pass series ------------------------------------------

def test_quest_with_run_time_gets_its_own_pass_task():
    client = FakeRunTimeClient(
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30",
                                   "run_timezone": "America/Los_Angeles"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.created) == 1
    created = client.created[0]
    assert created["task_kind"] == AUTOPILOT_PASS_KIND
    assert created["goal_id"] == "q1"
    assert created["recurrence"] == {"frequency": "daily", "time": "06:30"}
    assert created["scheduled_time"] == "06:30"
    assert "scheduled_date" in created


# --- 2/3: a quest that names no time is not a different case ---------------------------------

def test_quest_with_no_run_time_gets_its_own_pass_at_the_lane_default_hour():
    """The absent field is a default, not a branch: this quest gets exactly the same kind of
    series as one that named a time, fired at ``cfg.autopilot_pass_time``."""
    client = FakeRunTimeClient(
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller_now(client, NOW, autopilot_pass_time="07:00")._ensure_autopilot_pass()
    assert len(client.created) == 1
    created = client.created[0]
    assert created["goal_id"] == "q1"
    assert created["recurrence"] == {"frequency": "daily", "time": "07:00"}
    assert created["scheduled_time"] == "07:00"


def test_no_team_wide_pass_is_ever_created():
    client = FakeRunTimeClient(
        quests=[{"quest_id": "q1"}, {"quest_id": "q2"}],
        autopilot_by_quest={
            "q1": {"mode": "act", "run_time": "06:30"},
            "q2": {"mode": "suggest", "run_time": "20:00"},
        },
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert [t for t in client.created if "goal_id" not in t] == []
    assert len([t for t in client.created if t.get("goal_id") in ("q1", "q2")]) == 2


# --- 4: one open occurrence per quest is the liveness test ---------------------------------------

@pytest.mark.parametrize("status", sorted(OPEN_TASK_STATUSES))
def test_an_open_occurrence_for_one_quest_does_not_suppress_or_get_suppressed_by_another(status):
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="qA", status=status, scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "qA"}, {"quest_id": "qB"}],
        autopilot_by_quest={
            "qA": {"mode": "act", "run_time": "06:30", "last_pass_at": "2026-08-19T13:30:00Z"},
            "qB": {"mode": "act", "run_time": "07:00"},
        },
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    # qB, with no open occurrence of its own, gets one created regardless of qA's state.
    assert any(t.get("goal_id") == "qB" for t in client.created)
    # qA already has an open occurrence -- creating a SECOND one is exactly what must not happen.
    assert not any(t.get("goal_id") == "qA" for t in client.created)


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_a_terminal_occurrence_means_the_series_is_gone_so_recreate(status):
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="qA", status=status)],
        quests=[{"quest_id": "qA"}],
        autopilot_by_quest={"qA": {"mode": "act", "run_time": "06:30"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert any(t.get("goal_id") == "qA" for t in client.created)


# --- 5: retune, and the quiet steady state --------------------------------------------------

def test_retune_writes_once_when_the_run_time_changed():
    # cadence "daily" + last_pass_at YESTERDAY (UTC, no run_timezone here) -> due -> expected_date
    # is today, which already matches the occurrence -- isolating this test to the TIME change.
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "07:15", "cadence": "daily",
                                   "last_pass_at": "2026-08-19T13:00:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.task_updates) == 1
    task_id, fields = client.task_updates[0]
    assert task_id == "p1"
    assert fields["scheduled_time"] == "07:15"
    assert fields["recurrence"] == {"frequency": "daily", "time": "07:15"}
    assert "scheduled_date" not in fields    # only the keys that actually differ travel


def test_retune_is_silent_when_the_occurrence_already_matches():
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "last_pass_at": "2026-08-19T13:00:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.task_updates == []
    assert client.created == []


# --- 6: the UTC-midnight spawn-date correction ------------------------------------------------

def test_a_utc_midnight_spawn_is_corrected_back_to_tomorrow():
    """A 22:00 America/Los_Angeles quest whose pass just finished: the backend's
    ``next_occurrence_date`` used the UTC date, so the freshly spawned occurrence landed on the
    day-after-tomorrow in LA terms. The sweep must PATCH it back to tomorrow -- and touch nothing
    but the date, since the time itself never changed."""
    # NOW: 2026-08-20 09:00 UTC == 2026-08-20 02:00 America/Los_Angeles (PDT, UTC-7).
    # last_pass_at: ~20 minutes ago, still 2026-08-20 in LA -- the run that just finished.
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-22",
                          scheduled_time="22:00", recurrence_time="22:00")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "22:00", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-20T08:40:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.task_updates) == 1
    task_id, fields = client.task_updates[0]
    assert task_id == "p1"
    assert fields == {"scheduled_date": "2026-08-21"}  # tomorrow, LA terms -- only the date moved


# --- 7: catch-up ------------------------------------------------------------------------------

def test_catchup_expected_date_is_today_when_the_run_time_already_passed_and_not_run_today():
    # NOW: 2026-08-20 09:00 UTC == 2026-08-20 02:00 America/Los_Angeles. A 06:30 run today has NOT
    # happened yet in LA terms either, but the point stands regardless: last_pass_at is YESTERDAY.
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-19",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-19T10:00:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    task_id, fields = client.task_updates[0]
    assert fields["scheduled_date"] == "2026-08-20"  # today, not tomorrow -- catch up immediately


def test_catchup_expected_date_is_tomorrow_once_already_run_today_and_no_second_pass_is_created():
    # The occurrence is already dated tomorrow (2026-08-21) -- exactly what `last_pass_at` a few
    # minutes ago (same LA calendar day as NOW) computes as the expected date, so this is the
    # quiet steady state: no retune write, and critically, no SECOND pass created for the quest.
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-20T08:40:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []       # no second pass created for this quest
    assert client.task_updates == []  # already dated tomorrow -- nothing to correct


# --- 8: a 409 on retune is swallowed ------------------------------------------------------------

def test_a_409_on_retune_is_swallowed_and_leaves_the_occurrence_untouched():
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "07:15", "cadence": "daily",
                                   "last_pass_at": "2026-08-19T13:00:00Z"}},
        update_error=QuestApiError("schedule conflict", status=409),
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()      # must not raise
    # Nothing recorded as an update (the fake raises before appending), and nothing (re)created.
    assert client.task_updates == []
    assert client.created == []


# --- 9: retirement in ONE patch -----------------------------------------------------------------

def test_retiring_a_series_when_mode_goes_off_clears_recurrence_and_cancels_in_one_patch():
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "off", "run_time": "06:30"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.task_updates) == 1
    task_id, fields = client.task_updates[0]
    assert task_id == "p1"
    assert fields == {"recurrence": "", "status": "cancelled"}


def test_clearing_run_time_retunes_to_the_default_hour_instead_of_retiring():
    """Clearing the field is not "switch this quest off": the quest is still opted in, so its
    series stays and simply moves to the lane's default hour. Retirement is for mode off."""
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": ""}},
    )
    _poller_now(client, NOW, autopilot_pass_time="07:00")._ensure_autopilot_pass()
    assert len(client.task_updates) == 1
    task_id, fields = client.task_updates[0]
    assert task_id == "p1"
    assert fields.get("scheduled_time") == "07:00"
    assert "status" not in fields          # retuned, not retired
    assert client.created == []


def test_an_open_team_wide_pass_is_retired_and_replaced_by_the_quest_own_series():
    """Migration, so a deployment that already had a team-wide pass converges by itself rather
    than leaving an orphan firing daily forever. One PATCH: recurrence cleared AND cancelled."""
    client = FakeRunTimeClient(
        tasks=[{"id": "team1_pass", "task_kind": AUTOPILOT_PASS_KIND, "status": "queued"}],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.task_updates == [("team1_pass", {"recurrence": "", "status": "cancelled"})]
    assert len(client.created) == 1
    assert client.created[0]["goal_id"] == "q1"


# --- 10: duplicate open occurrences ---------------------------------------------------------

def test_two_open_occurrences_warns_and_acts_on_the_earliest_only(caplog):
    client = FakeRunTimeClient(
        tasks=[
            _pass_task("later", quest_id="q1", scheduled_date="2026-08-22",
                      scheduled_time="06:30", recurrence_time="06:30"),
            _pass_task("earlier", quest_id="q1", scheduled_date="2026-08-21",
                      scheduled_time="09:00", recurrence_time="06:30"),
        ],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30",
                                   "last_pass_at": "2026-08-19T13:00:00Z"}},
    )
    with caplog.at_level("WARNING"):
        _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []  # never create a third
    assert len(client.task_updates) == 1
    task_id, _fields = client.task_updates[0]
    assert task_id == "earlier"          # acted on the earliest scheduled_date only
    assert "2 open pass occurrences" in caplog.text
