"""The runner's guarantee that the recurring "Autopilot pass" task EXISTS.

Autopilot is implemented as a task rather than a daemon, which is what makes it visible,
pausable and auditable in the same UI as everything else. The hole was that nothing ever created
that task: a user could switch a quest to Suggest/Act, the setting saved correctly, and then
absolutely nothing happened, forever, with no error anywhere. These tests pin the runner-side
fix -- ``Poller._ensure_autopilot_pass`` -- including the cheap steady state (one list call when
the pass already exists) and the refusal to create one when no quest is opted in.
"""
import pytest

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.poller import Poller

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
        so a row owned by anyone but the caller is INVISIBLE there. ``owner`` on a fake task is
        that row's owner, and a fake client's own identity is ``self.user_id``. Second, a failed
        read is ``None``, not ``[]``.
        """
        self.list_tasks_calls.append({"team_id": team_id, "goal_id": goal_id,
                                      "task_kind": task_kind})
        if self.list_fails:
            return None
        rows = [t for t in self.tasks
                if task_kind is None or t.get("task_kind") == task_kind]
        if goal_id is not None:
            return [t for t in rows if t.get("goal_id") == goal_id]
        return [t for t in rows if t.get("owner", self.user_id) == self.user_id]

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
        record = {"id": f"pass_{len(self.created) + 1}", "text": text, **kwargs}
        self.created.append(record)
        return record


def _poller(client, **cfg_overrides):
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]),
        **cfg_overrides,
    )
    return Poller(cfg, state_path=None, client=client)


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
                "owner": "human_owner", "created_by": FakePassClient.user_id,
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
