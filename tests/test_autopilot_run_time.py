"""Per-quest ``run_time``: one pass series per opted-in quest, at its own hour.

There is no second kind of pass. A quest that names no ``run_time`` is given the lane's default
hour when its schedule is read, so it gets its own series like every other one; any surviving
team-wide series is retired. These tests pin that, the schedule-correction sweep
(A3: the backend spawns the next occurrence on a UTC date, which is wrong for a run time west
enough to cross UTC midnight), the catch-up formula, retirement, and duplicate handling -- all of
it against ``FakeRunTimeClient``, a thin extension of ``test_autopilot_pass_task``'s
``FakePassClient`` that adds ``update_task`` and a 409 knob.

Sections 13 and 14 pin the 2026-09-05 incident: a "Run now" on a quest whose pass had already run
that morning could not move the series onto a date the series already occupied, so it was silently
dropped. It now gets a one-off catch-up pass instead, and the two kinds of pass are told apart by
whether they carry a recurrence at all.
"""
from datetime import datetime, timezone

import pytest

from quest_ai_runner.runner.autopilot import AUTOPILOT_PASS_KIND, OPEN_TASK_STATUSES
from quest_ai_runner.runner.quest_client import QuestApiError

from .test_autopilot_pass_task import FakePassClient, _poller

NOW = datetime(2026, 8, 20, 9, 0, 0, tzinfo=timezone.utc)  # a Thursday


class FakeRunTimeClient(FakePassClient):
    """Adds ``update_task`` (retune/retire) and an optional error to raise from it, on top of
    ``FakePassClient``'s create/list/state-read behaviour.

    ``date_conflict=True`` models the backend's real constraint rather than a blanket failure: a
    series holds a unique index on (series_id, scheduled_date), so only a PATCH that MOVES the
    date can 409, and it does so whenever the target date is already taken. A test that raised on
    every PATCH could not tell the difference between "this date is spoken for" and "the API is
    down", which is the distinction the catch-up path turns on.
    """

    def __init__(self, *, update_error=None, date_conflict=False, **kwargs):
        super().__init__(**kwargs)
        self.update_error = update_error
        self.date_conflict = date_conflict
        self.task_updates = []  # (task_id, fields)

    def update_task(self, task_id, fields):
        if self.update_error:
            raise self.update_error
        if self.date_conflict and "scheduled_date" in fields:
            raise QuestApiError(
                "Series ser_x already has a task scheduled on "
                f"{fields['scheduled_date']} (task other_occurrence)", status=409)
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


# --- 12: "Run now" pulls the SAME occurrence forward --------------------------------------------

def test_a_pending_run_request_pulls_the_occurrence_to_today_even_after_a_pass_ran():
    """"Run now" on a quest that ALREADY ran today must still run.

    This is the case the button exists for, and the one the cadence gate would otherwise refuse:
    ``last_pass_at`` is earlier today, so the occurrence sits on tomorrow. A pending request
    (``run_requested_at`` newer than ``last_pass_at``) moves that same occurrence back to today.
    """
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-20T08:40:00Z",
                                   "run_requested_at": "2026-08-20T08:55:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []               # the same series, not a second one
    task_id, fields = client.task_updates[0]
    assert task_id == "p1"
    assert fields["scheduled_date"] == "2026-08-20"


def test_a_pending_run_request_pulls_the_time_back_to_now_when_the_run_time_is_still_ahead():
    """Requested at 02:00 local with a 06:30 run_time: waiting until 06:30 is not "run now".

    NOW is 2026-08-20 09:00 UTC == 02:00 America/Los_Angeles, so run_time is four hours away. The
    occurrence has to be dated at or before the current local time to be discovered as due.
    """
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-19T10:00:00Z",
                                   "run_requested_at": "2026-08-20T08:59:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    task_id, fields = client.task_updates[0]
    # The date was already today, so only the time is written -- the retune writes diffs only.
    assert "scheduled_date" not in fields
    assert fields["scheduled_time"] == "02:00"   # now, not the 06:30 that is still ahead


def test_a_spent_run_request_changes_nothing():
    """A request OLDER than ``last_pass_at`` has been answered: back to the ordinary schedule.

    This is what makes the request self-consuming -- the pass stamping ``last_pass_at`` is the
    only "clear" there is, so a stale stamp left on the quest forever must be inert.
    """
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-20T08:40:00Z",
                                   "run_requested_at": "2026-08-20T08:10:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []
    assert client.task_updates == []   # steady state: the request is spent, nothing to correct


def test_a_run_request_never_starts_a_series_for_a_quest_whose_autopilot_is_off():
    """Mode is the outer gate and a request does not override it. The backend refuses the request
    with a 409 for the same reason; this is the runner's half of that guarantee."""
    client = FakeRunTimeClient(
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "off", "run_time": "06:30",
                                   "run_requested_at": "2026-08-20T08:59:00Z"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []


# --- 13: "Run now" when the series CANNOT move -- the one-off catch-up pass -------------------
#
# Live on 2026-09-05: a "Run now" on a quest whose pass had already run that morning did nothing
# at all. The series' open occurrence was tomorrow's, the retune tried to move it onto today, and
# the backend's unique index on (series_id, scheduled_date) refused -- today's slot was held by
# the occurrence that ran and completed that morning. The retune logged the 409 and gave up, so
# the request slipped a day. These pin the fix: the pending request gets its OWN one-off pass
# instead, which respects the index rather than fighting it, and stays a one-off forever.

# A pending request on a quest that ALREADY ran this morning: the exact live shape.
RAN_THIS_MORNING = {"mode": "act", "run_time": "06:30", "cadence": "daily",
                    "run_timezone": "America/Los_Angeles",
                    "last_pass_at": "2026-08-20T08:40:00Z",
                    "run_requested_at": "2026-08-20T08:55:00Z",
                    "env_id": "env-x"}


def _catchup_task(task_id, *, quest_id, status="queued", scheduled_date="2026-08-20",
                  scheduled_time="02:00"):
    """A one-off catch-up: the pass kind and the quest's ``goal_id``, but NO recurrence. That
    absence is the whole of what makes it a one-off, so it is the whole of what these assert."""
    return {"id": task_id, "task_kind": AUTOPILOT_PASS_KIND, "status": status,
            "goal_id": quest_id, "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time}


def test_a_pending_run_the_series_cannot_move_gets_a_one_off_catchup_pass():
    client = FakeRunTimeClient(
        # Tomorrow's occurrence, because a pass already ran today. Moving it onto today is what
        # the backend refuses.
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": dict(RAN_THIS_MORNING)},
        date_conflict=True,
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.created) == 1
    created = client.created[0]
    assert created["task_kind"] == AUTOPILOT_PASS_KIND
    assert created["goal_id"] == "q1"
    assert created["env_id"] == "env-x"          # the quest's own env, like every other pass
    assert "recurrence" not in created           # a one-off: no series, no next occurrence
    # NOW is 02:00 in the quest's zone -- scheduled for now, so the next scan discovers it as due.
    assert created["scheduled_date"] == "2026-08-20"
    assert created["scheduled_time"] == "02:00"
    assert client.task_updates == []             # the series' own schedule is left untouched


def test_a_second_scan_creates_no_further_catchup_while_one_is_still_open():
    """The request stays pending until a pass stamps ``last_pass_at``, so every scan in between
    sees the same conflict. One catch-up is the answer; a pile of them is not."""
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30"),
               _catchup_task("c1", quest_id="q1")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": dict(RAN_THIS_MORNING)},
        date_conflict=True,
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []


def test_a_catchup_is_never_retuned_and_never_gains_a_recurrence(caplog):
    """Retuning one is what silently converted the hand-made catch-up into a second series: an
    absent recurrence differs from the expected one, so the retune would stamp a daily one on it.
    A quest in its quiet steady state with a stale catch-up beside it must write nothing at all,
    and must not report the catch-up as a duplicate occurrence either."""
    client = FakeRunTimeClient(
        # last_pass_at is this morning and no request is pending, so the expected occurrence is
        # tomorrow -- which is exactly where the series already sits. Nothing to retune.
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30"),
               _catchup_task("c1", quest_id="q1", scheduled_date="2026-08-19",
                             scheduled_time="23:00")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-20T08:40:00Z"}},
    )
    with caplog.at_level("WARNING"):
        _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.task_updates == []             # the catch-up was never PATCHed
    assert client.created == []
    assert "open pass occurrences" not in caplog.text


def test_a_quest_whose_only_open_pass_is_a_catchup_still_gets_its_series_created():
    """A catch-up never stands in for the series. Counting one as the quest's pass would leave the
    quest with a single run and no producer the moment that run closed."""
    client = FakeRunTimeClient(
        tasks=[_catchup_task("c1", quest_id="q1")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30",
                                   "run_timezone": "America/Los_Angeles"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert len(client.created) == 1
    assert client.created[0]["goal_id"] == "q1"
    assert client.created[0]["recurrence"] == {"frequency": "daily", "time": "06:30"}
    assert client.task_updates == []             # and the catch-up itself is still untouched


def test_a_date_conflict_with_no_pending_run_still_just_logs_and_changes_nothing():
    """The catch-up is for an explicit request, not for every conflict. An ordinary late run has
    the next sweep to re-evaluate it, and inventing an extra pass there would double the day."""
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-19",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act", "run_time": "06:30", "cadence": "daily",
                                   "run_timezone": "America/Los_Angeles",
                                   "last_pass_at": "2026-08-19T10:00:00Z"}},
        date_conflict=True,
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert client.created == []
    assert client.task_updates == []


def test_a_non_409_retune_failure_creates_no_catchup_even_with_a_pending_run():
    """Only the date conflict has a second course of action. Any other failure is the next scan's
    retry, and treating it as one would turn a transient API error into a stream of tasks."""
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-21",
                          scheduled_time="06:30", recurrence_time="06:30")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": dict(RAN_THIS_MORNING)},
        update_error=QuestApiError("upstream unavailable", status=503),
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()      # must not raise
    assert client.created == []
    assert client.task_updates == []


# --- 14: retirement can never re-spawn what it retired ------------------------------------------

def test_retirement_never_issues_a_bare_status_patch_that_would_respawn_the_series():
    """Pinned after 2026-09-05, when cancelling a recurring pass by status alone spawned its next
    occurrence and the "retired" series came straight back. Clearing the recurrence in the SAME
    PATCH as the status is what stops it, and it must hold for a catch-up too."""
    client = FakeRunTimeClient(
        tasks=[_pass_task("p1", quest_id="q1", scheduled_date="2026-08-20",
                          scheduled_time="06:30", recurrence_time="06:30"),
               _catchup_task("c1", quest_id="q1")],
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "off", "run_time": "06:30"}},
    )
    _poller_now(client, NOW)._ensure_autopilot_pass()
    assert sorted(tid for tid, _f in client.task_updates) == ["c1", "p1"]
    # Every PATCH that ends a task clears its recurrence in the same call. No exceptions, and no
    # earlier status-only PATCH for a later one to "fix".
    assert all(fields.get("recurrence") == "" for _tid, fields in client.task_updates
               if "status" in fields)
    assert all(fields == {"recurrence": "", "status": "cancelled"}
               for _tid, fields in client.task_updates)
