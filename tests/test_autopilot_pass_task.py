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

    def __init__(self, *, tasks=None, quests=None, autopilot_by_quest=None, create_error=None):
        self.tasks = list(tasks or [])
        self.quests = list(quests or [])
        self.autopilot_by_quest = dict(autopilot_by_quest or {})
        self.create_error = create_error
        self.created = []
        self.list_tasks_calls = []
        self.state_reads = []

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, source=None, task_kind=None):
        self.list_tasks_calls.append({"team_id": team_id, "task_kind": task_kind})
        return [t for t in self.tasks
                if task_kind is None or t.get("task_kind") == task_kind]

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
    steady state. What stays true, and is still the point of this test: the ``list_tasks`` call
    for existing pass occurrences stays ONE call regardless of how many quests exist.
    """
    client = FakePassClient(
        tasks=[{"id": "p1", "task_kind": "autopilot", "status": status, "goal_id": "q1"}],
        quests=[{"quest_id": "q1"}], autopilot_by_quest={"q1": {"mode": "act"}},
    )
    _poller(client)._ensure_autopilot_pass()
    assert client.created == []
    assert len(client.list_tasks_calls) == 1  # one list_tasks call, regardless of quest count
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
