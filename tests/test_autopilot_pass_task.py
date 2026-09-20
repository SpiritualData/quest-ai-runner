"""The runner's guarantee that the recurring "Autopilot pass" task EXISTS.

Autopilot is implemented as a task rather than a daemon, which is what makes it visible,
pausable and auditable in the same UI as everything else. The hole was that nothing ever created
that task: a user could switch a quest to Suggest/Act, the setting saved correctly, and then
absolutely nothing happened, forever, with no error anywhere. These tests pin the runner-side
fix -- ``Poller._ensure_autopilot_pass`` -- including the cheap steady state (one list call when
the pass already exists) and the refusal to create one when no quest is opted in.
"""
from datetime import datetime, timezone

import pytest

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.poller import Poller, _foreign_series_looks_alive

from .conftest import StubProvider, StubRetrieval


class FakePassClient:
    """The narrow slice of QuestClient that ``_ensure_autopilot_pass`` touches."""

    configured = True
    user_id = "acct_app"   # the lane's own account, the identity the team-wide listing scopes to

    def __init__(self, *, tasks=None, quests=None, autopilot_by_quest=None, create_error=None,
                 list_fails=False):
        self.tasks = list(tasks or [])
        self.quests = list(quests or [])
        self.autopilot_by_quest = dict(autopilot_by_quest or {})
        self.create_error = create_error
        # Models the real client's failure value: a read that could not be made answers None,
        # never an empty list. Set it to make EVERY listing fail, as a rate-limited lane does.
        self.list_fails = list_fails
        self.created = []
        self.list_tasks_calls = []
        self.state_reads = []

    def list_tasks_or_none(self, *, team_id=None, status=None, goal_id=None, source=None,
                           task_kind=None):
        """The failure-reporting listing, faked the way the real route BEHAVES.

        Two properties are load-bearing and a stub that ignored them would pass tests the real
        API fails (2026-09-17). First, ``goal_id`` is answered by the quest-scoped listing, so a
        row is returned whoever owns it; the team-wide listing (no ``goal_id``) is owner-scoped,
        so a row owned by anyone but the caller is INVISIBLE there. A row's own ``user_id`` is
        its owner (the field name the API really returns), and a fake client's own identity is
        ``self.user_id``. Second, a failed read is ``None``, not ``[]``.
        """
        self.list_tasks_calls.append({"team_id": team_id, "goal_id": goal_id,
                                      "task_kind": task_kind})
        if self.list_fails:
            return None
        rows = [t for t in self.tasks
                if task_kind is None or t.get("task_kind") == task_kind]
        if goal_id is not None:
            return [t for t in rows if t.get("goal_id") == goal_id]
        return [t for t in rows if t.get("user_id", self.user_id) == self.user_id]

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, source=None, task_kind=None):
        return self.list_tasks_or_none(team_id=team_id, status=status, goal_id=goal_id,
                                       source=source, task_kind=task_kind) or []

    def list_quests(self, team_id=None):
        return list(self.quests)

    def get_quest_autopilot(self, quest_id):
        self.state_reads.append(quest_id)
        return {"quest_id": quest_id,
                "autopilot": dict(self.autopilot_by_quest.get(quest_id, {}))}

    def create_task(self, text, **kwargs):
        if self.create_error:
            raise self.create_error
        # A None argument is dropped, exactly as the real client drops it from the POST body, so
        # "the lane sent no assignee" and "the lane sent None" cannot look the same in a test.
        sent = {k: v for k, v in kwargs.items() if v is not None}
        record = {"id": f"pass_{len(self.created) + 1}", "text": text, **sent}
        self.created.append(record)
        return record


def _poller(client, *, now=None, **cfg_overrides):
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]),
        **cfg_overrides,
    )
    return Poller(cfg, state_path=None, client=client, now=now)


def test_creates_a_recurring_pass_task_when_a_quest_is_opted_in_and_none_exists():
    client = FakePassClient(
        quests=[{"quest_id": "q1"}],
        autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller(client, env_id="env-personal")._ensure_autopilot_pass()
    assert len(client.created) == 1
    created = client.created[0]
    assert created["task_kind"] == "autopilot"          # the PASS kind: routed to AutopilotPass
    # A human-readable title, distinct from the technical instruction text: without one, the
    # frontend falls back to deriving a title FROM that text and truncates it into unreadable
    # noise ("Autopilot pass: scan this team's opted-in quests and make..."). See taskTitle.ts.
    assert created["title"] == "Autopilot pass"
    assert created["recurrence"] == {"frequency": "daily", "time": "07:00"}
    assert created["scheduled_time"] == "07:00"
    assert created["team_id"] == "team1"
    assert created["env_id"] == "env-personal"


def test_suggest_mode_also_counts_as_opted_in():
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "suggest"}})
    _poller(client)._ensure_autopilot_pass()
    assert len(client.created) == 1


def test_creates_nothing_when_no_quest_is_opted_in():
    client = FakePassClient(quests=[{"quest_id": "q1"}, {"quest_id": "q2"}],
                            autopilot_by_quest={"q1": {"mode": "off"}, "q2": {}})
    _poller(client)._ensure_autopilot_pass()
    assert client.created == []


@pytest.mark.parametrize("status", ["queued", "in_progress", "needs_you", "suggested"])
def test_an_open_pass_task_is_the_liveness_test_for_the_whole_series(status):
    """A recurring series always has exactly one occurrence outstanding (the backend spawns the
    next when the current reaches a terminal status), so one open occurrence means the series is
    alive: no second team pass is created.

    The per-quest schedule snapshot still reads each quest's autopilot state every time this
    method runs -- it needs to, to retune or retire a series -- so this is no longer a zero-read
    steady state, and since 2026-09-17 the liveness read is one listing per OPTED-IN quest plus
    the team-wide one (the team-wide list is owner-scoped and cannot answer the question for a
    quest somebody else owns). What stays true, and is the point of this test: an open occurrence
    means the series is alive and nothing is created.

    The occurrence carries a ``recurrence`` because a real one always does: the backend's spawner
    copies the field onto the next occurrence, and it is what marks this as part of a SERIES
    rather than a one-off catch-up pass (see ``Poller._split_pass_occurrences``).
    """
    client = FakePassClient(
        tasks=[{"id": "p1", "task_kind": "autopilot", "status": status, "goal_id": "q1",
                "recurrence": {"frequency": "daily", "time": "07:00"}}],
        quests=[{"quest_id": "q1"}], autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller(client)._ensure_autopilot_pass()
    assert client.created == []
    # The team-wide read (legacy team pass) plus one quest-scoped read for the opted-in quest.
    assert client.list_tasks_calls == [
        {"team_id": "team1", "goal_id": None, "task_kind": "autopilot"},
        {"team_id": None, "goal_id": "q1", "task_kind": "autopilot"},
    ]
    assert client.state_reads == ["q1"]       # the schedule snapshot still reads every quest once


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_only_terminal_pass_tasks_means_the_series_is_gone_so_recreate(status):
    client = FakePassClient(
        tasks=[{"id": "p1", "task_kind": "autopilot", "status": status}],
        quests=[{"quest_id": "q1"}], autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller(client)._ensure_autopilot_pass()
    assert len(client.created) == 1


def test_autopilot_work_tasks_are_not_mistaken_for_the_pass_task():
    """``autopilot_work`` is what a pass CREATES; it never does the scanning. Counting one as the
    pass would leave the quest with work but no producer once that work closed."""
    client = FakePassClient(
        tasks=[{"id": "w1", "task_kind": "autopilot_work", "status": "queued"}],
        quests=[{"quest_id": "q1"}], autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller(client)._ensure_autopilot_pass()
    assert len(client.created) == 1


def test_disabled_by_config_creates_nothing():
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    _poller(client, autopilot_ensure_pass_task=False)._ensure_autopilot_pass()
    assert client.created == []
    assert client.list_tasks_calls == []


def test_a_create_failure_is_swallowed_so_the_scan_still_runs():
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}},
                            create_error=RuntimeError("422 unknown field"))
    _poller(client)._ensure_autopilot_pass()      # must not raise
    assert client.created == []


def test_pass_time_is_configurable():
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    _poller(client, autopilot_pass_time="05:30")._ensure_autopilot_pass()
    assert client.created[0]["recurrence"]["time"] == "05:30"
    assert client.created[0]["scheduled_time"] == "05:30"


# --- ownership and failed reads (incident 2026-09-17) -------------------------------------------

def test_a_pass_owned_by_the_quests_human_owner_is_still_seen_so_no_duplicate_is_created():
    """The duplicate-series bug, pinned.

    A pass created against a quest is OWNED by that quest's owner; the account that created it is
    only ``created_by``. The team-wide listing is owner-scoped, so on a quest owned by a human the
    lane's own account got an empty answer, read it as "no pass exists", and created another
    recurring series on every scan (three in six minutes, live, each one a weekly brief). The
    quest-scoped listing is not owner-scoped, so it returns the row regardless, and the liveness
    test comes out right.
    """
    pass_row = {"id": "p1", "task_kind": "autopilot", "status": "queued", "goal_id": "q1",
                "user_id": "human_owner", "created_by": FakePassClient.user_id,
                "recurrence": {"frequency": "daily", "time": "07:00"},
                "scheduled_time": "07:00"}
    client = FakePassClient(tasks=[pass_row], quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    # The premise: this row is genuinely invisible in the owner-scoped team-wide listing.
    assert client.list_tasks(team_id="team1", task_kind="autopilot") == []
    client.list_tasks_calls.clear()

    _poller(client)._ensure_autopilot_pass()

    assert client.created == []
    assert {"team_id": None, "goal_id": "q1", "task_kind": "autopilot"} in client.list_tasks_calls


def test_a_failed_listing_never_creates_a_pass():
    """A read that could not be made is not an empty quest.

    ``list_tasks`` returns ``[]`` on any error, which is why a rate-limited half hour (429s) was
    indistinguishable from "no pass exists" and produced a duplicate series per scan. The liveness
    read now goes through ``list_tasks_or_none``, and ``None`` means: skip this quest, retry next
    scan, create nothing.
    """
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}},
                            list_fails=True)
    _poller(client)._ensure_autopilot_pass()      # must not raise
    assert client.created == []


def test_a_failed_listing_does_not_retire_the_team_wide_pass_either():
    """A failed team-wide read must not be mistaken for "no team pass is left"; it is simply not
    an answer, and nothing is acted on until a scan gets a real one."""
    client = FakePassClient(
        tasks=[{"id": "team_pass", "task_kind": "autopilot", "status": "queued", "goal_id": None}],
        quests=[{"quest_id": "q1"}], autopilot_by_quest={"q1": {"mode": "off"}},
        list_fails=True)
    client.update_calls = []
    client.update_task = lambda task_id, fields: client.update_calls.append((task_id, fields))
    _poller(client)._ensure_autopilot_pass()
    assert client.update_calls == []


# --- who the pass is created FOR (2026-09-17, the other half of the ownership fix) --------------

def test_a_pass_is_created_assigned_to_the_lanes_own_account():
    """Seeing the pass was only half of it: it still had to RUN.

    The backend makes the linked quest's OWNER the executor of a goal-linked task, and task
    discovery is owner-scoped, so on a human-owned quest the pass this lane created sat queued
    forever and no lane ever discovered it (verified live: discover_due returned 0 tasks while
    three queued passes sat on the quest). Sending the lane's own account as ``assignee_user_id``
    makes that account the executor, which is what discovery scopes on.
    """
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    _poller(client, lane_user_id=FakePassClient.user_id)._ensure_autopilot_pass()
    assert client.created[0]["assignee_user_id"] == FakePassClient.user_id


def test_no_assignee_is_sent_when_the_lane_does_not_know_its_own_account():
    """``lane_user_id`` is operator config (an API key cannot ask /api/auth/me who it is), so an
    existing deployment that never sets it must behave byte-for-byte as before: no assignee field,
    the backend's default executor stands."""
    client = FakePassClient(quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    _poller(client)._ensure_autopilot_pass()
    assert "assignee_user_id" not in client.created[0]


def test_a_pass_owned_by_someone_else_is_reported_and_replaced(caplog):
    """A foreign-owned pass is INERT, so it must not stand in for the quest's series.

    Passes created BEFORE the assignee fix are owned by the quest's human owner. The lane can
    neither run them (discovery is scoped to the caller's own user_id) nor cancel them (the task
    PATCH is owner-scoped and 404s). Treating one as "the series exists" therefore leaves the quest
    with no runnable pass at all, forever and silently, which is exactly the failure this whole
    area keeps producing. So the lane ignores it for liveness and creates its own, while still
    naming the id every scan so a human can clear the dead row when convenient.
    """
    foreign = {"id": "p_old", "task_kind": "autopilot", "status": "queued", "goal_id": "q1",
               "user_id": "human_owner", "created_by": FakePassClient.user_id,
               "recurrence": {"frequency": "daily", "time": "07:00"},
               "scheduled_date": "2026-09-17", "scheduled_time": "07:00"}
    client = FakePassClient(tasks=[foreign], quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    client.update_calls = []
    client.update_task = lambda task_id, fields: client.update_calls.append((task_id, fields))

    with caplog.at_level("WARNING"):
        _poller(client, lane_user_id=FakePassClient.user_id)._ensure_autopilot_pass()

    assert len(client.created) == 1    # the quest gets a pass this lane can actually run
    assert client.created[0]["assignee_user_id"] == FakePassClient.user_id
    assert client.update_calls == []   # and never retry a PATCH that can only 404
    assert "p_old" in caplog.text      # the human still gets the id, to clear the dead row


# --- a foreign series that still looks ALIVE (2026-09-20, multi-owner quests) --------------------
#
# The blanket "a foreign occurrence is always inert, always create our own" trade above was safe
# only while one app account ran exactly one lane. On a quest with multiple owner_user_ids, a
# human owner's personal lane and the shared org lane can both actively service the SAME quest, and
# each independently reads the other's series as a dead row -- three separately-worded autopilot
# reports (and three emails) for one quest inside ten minutes, live. The fix distinguishes a
# foreign series another lane is actually running (do not create a second one) from one that is
# genuinely abandoned (create ours, exactly as before).

def test_a_foreign_alive_series_blocks_this_lane_from_creating_a_second_series(caplog):
    """A foreign SERIES occurrence that is not overdue (scheduled today or later) reads as another
    account's lane actively servicing this quest, so this lane must NOT start a competing series.
    The warning must say why, distinctly from the generic foreign-occurrence warning above, since
    that is what tells a human whether there is anything to clear."""
    foreign = {"id": "p_alive", "task_kind": "autopilot", "status": "queued", "goal_id": "q1",
               "user_id": "other_owner", "created_by": FakePassClient.user_id,
               "recurrence": {"frequency": "daily", "time": "07:00"},
               "scheduled_date": "2026-09-21", "scheduled_time": "07:00"}
    client = FakePassClient(tasks=[foreign], quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    now = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        _poller(client, lane_user_id=FakePassClient.user_id, now=now)._ensure_autopilot_pass()

    assert client.created == []        # no competing series
    assert "already be served by another account" in caplog.text


def test_a_foreign_stale_series_still_lets_this_lane_create_its_own(caplog):
    """A foreign SERIES occurrence that is overdue (scheduled in the past, nothing advancing it)
    still reads as abandoned, so this lane creates its own -- the pre-existing behaviour this fix
    must not regress."""
    foreign = {"id": "p_stale", "task_kind": "autopilot", "status": "queued", "goal_id": "q1",
               "user_id": "other_owner", "created_by": FakePassClient.user_id,
               "recurrence": {"frequency": "daily", "time": "07:00"},
               "scheduled_date": "2026-09-10", "scheduled_time": "07:00"}
    client = FakePassClient(tasks=[foreign], quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    now = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        _poller(client, lane_user_id=FakePassClient.user_id, now=now)._ensure_autopilot_pass()

    assert len(client.created) == 1
    assert client.created[0]["assignee_user_id"] == FakePassClient.user_id
    assert "p_stale" in caplog.text    # still named, for a human to clear


def test_a_foreign_catchup_only_never_blocks_creation():
    """A foreign occurrence with no ``recurrence`` is a one-off catch-up, never a series, so it
    must never hold off creating the quest's own series -- only a foreign SERIES gets a say."""
    foreign_catchup = {"id": "p_catchup", "task_kind": "autopilot", "status": "queued",
                       "goal_id": "q1", "user_id": "other_owner",
                       "created_by": FakePassClient.user_id, "scheduled_date": "2026-09-25",
                       "scheduled_time": "07:00"}
    client = FakePassClient(tasks=[foreign_catchup], quests=[{"quest_id": "q1"}],
                            autopilot_by_quest={"q1": {"mode": "act"}})
    now = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    _poller(client, lane_user_id=FakePassClient.user_id, now=now)._ensure_autopilot_pass()

    assert len(client.created) == 1
    assert client.created[0]["assignee_user_id"] == FakePassClient.user_id


def test_foreign_series_liveness_falls_back_to_updated_at_when_scheduled_date_is_missing():
    """Missing/unparseable ``scheduled_date`` falls back to ``updated_at`` recency, and missing
    both defaults to alive -- the conservative choice, since the generic foreign-occurrence warning
    already tells a human there is something to look at."""
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    entry = {}
    recent = {"updated_at": "2026-09-19T12:00:00Z"}
    stale = {"updated_at": "2026-08-01T12:00:00Z"}
    unparseable = {"updated_at": "not-a-date"}
    nothing = {}

    assert _foreign_series_looks_alive(recent, entry, now) is True
    assert _foreign_series_looks_alive(stale, entry, now) is False
    assert _foreign_series_looks_alive(unparseable, entry, now) is True
    assert _foreign_series_looks_alive(nothing, entry, now) is True


# --- one lane, several teams (RunnerConfig.team_ids) --------------------------------------------

class MultiTeamPassClient(FakePassClient):
    """A pass client whose quest listing is actually TEAM-SCOPED, like the real route.

    ``FakePassClient`` returns the same quests for every team, which cannot tell a pass created on
    the right team apart from one created on the wrong team. Here each quest belongs to exactly
    one team, and a team whose id is in ``fail_list_for_teams`` fails its pass listing the way a
    rate-limited lane does (``None``, never ``[]``).
    """

    def __init__(self, *, quests_by_team=None, fail_list_for_teams=(), **kw):
        super().__init__(**kw)
        self.quests_by_team = dict(quests_by_team or {})
        self.fail_list_for_teams = set(fail_list_for_teams)
        self.quest_list_teams = []

    def list_quests(self, team_id=None):
        self.quest_list_teams.append(team_id)
        return [dict(q) for q in self.quests_by_team.get(team_id, [])]

    def list_tasks_or_none(self, *, team_id=None, status=None, goal_id=None, source=None,
                           task_kind=None):
        if team_id in self.fail_list_for_teams:
            self.list_tasks_calls.append({"team_id": team_id, "goal_id": goal_id,
                                          "task_kind": task_kind})
            return None
        return super().list_tasks_or_none(team_id=team_id, status=status, goal_id=goal_id,
                                          source=source, task_kind=task_kind)


def test_a_quests_pass_is_created_on_that_quests_own_team():
    """THE correctness fix this whole change turns on.

    ``_create_quest_pass`` used to hardcode the lane's home team. On a lane serving several teams
    that files team2's quest's pass on team1 -- a task team2's own people cannot see, pause or
    audit, attached to a quest that is not on that team at all. The schedule snapshot records
    which team each quest was listed under; the pass must be created there.
    """
    client = MultiTeamPassClient(
        quests_by_team={"team1": [{"quest_id": "q_home"}], "team2": [{"quest_id": "q_away"}]},
        autopilot_by_quest={"q_home": {"mode": "act"}, "q_away": {"mode": "act"}},
    )
    _poller(client, team_ids=["team1", "team2"])._ensure_autopilot_pass()

    by_quest = {c["goal_id"]: c for c in client.created}
    assert by_quest["q_home"]["team_id"] == "team1"
    assert by_quest["q_away"]["team_id"] == "team2"   # NOT the lane's home team


def test_a_catchup_pass_also_lands_on_the_quests_own_team():
    """The "Run now" one-off goes through a second creation path, and an inconsistency between
    the two would be invisible until somebody pressed the button."""
    client = MultiTeamPassClient(
        quests_by_team={"team1": [], "team2": [{"quest_id": "q_away"}]},
        autopilot_by_quest={"q_away": {"mode": "act"}},
    )
    poller = _poller(client, team_ids=["team1", "team2"])
    entry = poller._quest_schedule_snapshot()["q_away"]
    poller._create_quest_catchup_pass("q_away", entry)
    assert client.created[0]["team_id"] == "team2"


def test_the_schedule_snapshot_merges_every_team_and_remembers_whose_quest_is_whose():
    client = MultiTeamPassClient(
        quests_by_team={"team1": [{"quest_id": "q1"}],
                        "team2": [{"quest_id": "q2"}],
                        "team3": [{"quest_id": "q3"}]},
        autopilot_by_quest={"q1": {"mode": "act"}, "q2": {"mode": "suggest"},
                            "q3": {"mode": "off"}},
    )
    snapshot = _poller(client, team_ids=["team1", "team2", "team3"])._quest_schedule_snapshot()

    assert set(snapshot) == {"q1", "q2", "q3"}          # every team's quests, one map
    assert snapshot["q1"]["team_id"] == "team1"
    assert snapshot["q2"]["team_id"] == "team2"
    assert snapshot["q3"]["team_id"] == "team3"         # recorded even for a quest not opted in
    assert client.quest_list_teams == ["team1", "team2", "team3"]


def test_a_single_team_lane_lists_quests_exactly_as_before():
    """No ``team_ids``: one listing, on the home team, with the home team recorded. This is the
    no-op proof for the snapshot half of the change."""
    client = MultiTeamPassClient(
        quests_by_team={"team1": [{"quest_id": "q1"}]},
        autopilot_by_quest={"q1": {"mode": "act"}},
    )
    snapshot = _poller(client)._quest_schedule_snapshot()
    assert client.quest_list_teams == ["team1"]
    assert snapshot["q1"]["team_id"] == "team1"


def test_one_teams_failed_pass_listing_voids_the_whole_merged_read():
    """A failed read is not an empty one (incident, 2026-09-17).

    The merged team-wide listing drives RETIREMENT. A partial list read as complete would retire
    a live series that merely sat on the team whose read failed. So any failure makes the whole
    answer ``None``, the sweep declines to retire anything, and the lane retries next scan.
    """
    client = MultiTeamPassClient(
        quests_by_team={"team1": [{"quest_id": "q1"}], "team2": [{"quest_id": "q2"}]},
        autopilot_by_quest={"q1": {"mode": "act"}, "q2": {"mode": "act"}},
        fail_list_for_teams=["team2"],
    )
    poller = _poller(client, team_ids=["team1", "team2"])
    assert poller._list_legacy_pass_tasks() is None


def test_a_failed_team_read_does_not_stop_the_other_teams_quests_getting_a_pass(caplog):
    """The ``None`` is about RETIRING, not about creating: each quest's own liveness read is
    per-quest and unaffected, so the teams that answered still converge this scan."""
    client = MultiTeamPassClient(
        quests_by_team={"team1": [{"quest_id": "q1"}], "team2": [{"quest_id": "q2"}]},
        autopilot_by_quest={"q1": {"mode": "act"}, "q2": {"mode": "act"}},
        fail_list_for_teams=["team2"],
    )
    with caplog.at_level("WARNING"):
        _poller(client, team_ids=["team1", "team2"])._ensure_autopilot_pass()
    assert {c["goal_id"] for c in client.created} == {"q1", "q2"}
    assert "not retiring anything" in caplog.text
