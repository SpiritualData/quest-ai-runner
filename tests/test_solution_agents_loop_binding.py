"""``solution_agents_loop``: the sub-agents of a task are the solutions of the task's own quest, read from Quest per run. Offline: a stub client stands in for Quest."""
from __future__ import annotations

from quest_ai_runner.the_world.tools.delegated_agent_sessions.solution_agents_loop_runner import (
    SolutionAgentsLoopRunner, quest_of, quest_solution_rows, solution_specs)
from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessConfig

SPEC = {"records": [
    {"type": "problem", "recordId": "p1", "fields": {"problem": "x"}},
    {"type": "solution", "recordId": "sol-reading", "fields": {"name": "Reading", "purpose": "Read the sources", "approach": "Read each one", "out_of_scope": "Writing",
                                                               "role": "gather", "delegates_to": "sol-notes"},
     "milestoneEntries": [{"sentence": "All sources read", "met_at": "2026-01-01"}, {"sentence": "Summaries written", "met_at": None}]},
    {"type": "solution", "recordId": "sol-notes", "fields": {"name": "Notes", "purpose": "Keep notes", "approach": "One page each", "milestones": "1. Notes started"}},
]}


class Client:
    def __init__(self, spec):
        self.spec, self.asked = spec, []

    def get_spec(self, subject_type, subject_id):
        self.asked.append((subject_type, subject_id))
        return self.spec


def test_the_brief_names_the_quest():
    assert quest_of("x\nThis run's quest is quest_abc123: add --quest quest_abc123 to a call") == "quest_abc123"
    assert quest_of("a brief with no quest") == ""


def test_rows_carry_the_next_unmet_milestone_and_every_field():
    rows = quest_solution_rows(SPEC)
    assert set(rows) == {"sol-reading", "sol-notes"}
    assert rows["sol-reading"]["milestones"] == "1. Summaries written" and rows["sol-reading"]["delegates_to"] == "sol-notes"
    assert rows["sol-notes"]["milestones"] == "1. Notes started"


def make_runner(spec):
    return SolutionAgentsLoopRunner(SubprocessConfig(working_dir="/tmp"), client_factory=lambda: Client(spec))


def test_the_quests_solutions_are_the_agents_of_the_run():
    runner = make_runner(SPEC)
    seen = {}
    original = SolutionAgentsLoopRunner.__mro__[1].prepare

    def spy(self, goal, brief, preamble, root, key):
        seen["specs"] = self.solutions(goal, root)
        seen["network"] = self.records
    SolutionAgentsLoopRunner.__mro__[1].prepare = spy
    try:
        runner.prepare("goal", "This run's quest is quest_abc123: add", "", "/tmp", "k")
    finally:
        SolutionAgentsLoopRunner.__mro__[1].prepare = original
    assert {s.solution: s.kind for s in seen["specs"]} == {"sol-reading": "gather", "sol-notes": "work"}
    assert next(s for s in seen["specs"] if s.solution == "sol-reading").tools == ("Read", "Glob", "Grep")
    net = seen["network"]
    assert net.has("sol-reading") and net.name("sol-reading") == "Reading"
    assert runner.records is None and runner.solutions("goal", "/tmp") == []        # nothing is kept after the run is prepared


def test_no_solutions_gives_the_hub_fallback():
    runner = make_runner({"records": [{"type": "problem", "recordId": "p1", "fields": {"problem": "x"}}]})
    assert runner.quest_solutions("goal", "/tmp") == []
    assert quest_solution_rows({"records": []}) == {}
    assert solution_specs(None, {}, "goal") == []


def test_a_brief_with_no_quest_reads_nothing():
    client = Client(SPEC)
    runner = SolutionAgentsLoopRunner(SubprocessConfig(working_dir="/tmp"), client_factory=lambda: client)
    original = SolutionAgentsLoopRunner.__mro__[1].prepare
    SolutionAgentsLoopRunner.__mro__[1].prepare = lambda self, *a: None
    try:
        runner.prepare("goal", "no quest named here", "", "/tmp", "k")
    finally:
        SolutionAgentsLoopRunner.__mro__[1].prepare = original
    assert client.asked == []


def test_the_run_names_its_quest_as_data(monkeypatch):
    """``run_goal`` takes ``quest_id``; ``prepare`` reads it first and the brief's sentence only when it is absent; the deep run offers it to this runner alone."""
    import inspect
    from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import SubprocessGoalRunner
    from quest_ai_runner.the_intelligence.mind.delegation.orchestrator_capabilities import run_goal_accepts_quest_id
    assert run_goal_accepts_quest_id(make_runner(SPEC)) and not run_goal_accepts_quest_id(SubprocessGoalRunner(SubprocessConfig(working_dir="/w")))

    class Loose:
        def run_goal(self, **kwargs):
            return None
    assert not run_goal_accepts_quest_id(Loose())            # a bare **kwargs is not an opt-in
    hub = SolutionAgentsLoopRunner.__mro__[1]
    assert "quest_id" not in inspect.signature(hub.run_goal).parameters
    client = Client(SPEC)
    runner = SolutionAgentsLoopRunner(SubprocessConfig(working_dir="/tmp"), client_factory=lambda: client)
    monkeypatch.setattr(hub, "prepare", lambda self, *a: None)
    monkeypatch.setattr(hub, "run_goal", lambda self, **kw: runner.prepare(kw["goal"], kw["brief"], "", "/tmp", "k"))
    runner.run_goal(goal="g", brief="This run's quest is quest_fromthebrief: add", quest_id="quest_fromdata")
    assert client.asked == [("quest", "quest_fromdata")]
    runner.run_goal(goal="g", brief="This run's quest is quest_fromthebrief: add")
    assert client.asked[-1] == ("quest", "quest_fromthebrief")
    assert runner.view.quest_id == ""


def test_a_failure_inside_the_hubs_own_prepare_runs_it_once_and_surfaces_its_error(monkeypatch):
    """The quest read is guarded; the hub's prepare is not. Re-running it on its own failure repeated its side effects and hid the real error."""
    import pytest
    calls = []

    def failing(self, *a):
        calls.append(a)
        raise RuntimeError("hub prepare failed")
    monkeypatch.setattr(SolutionAgentsLoopRunner.__mro__[1], "prepare", failing)
    runner = make_runner(SPEC)
    with pytest.raises(RuntimeError, match="hub prepare failed"):
        runner.prepare("goal", "This run's quest is quest_abc123: add", "", "/tmp", "k")
    assert len(calls) == 1
    assert runner.records is None


def test_a_quest_that_cannot_be_read_still_prepares_the_hub_once(monkeypatch):
    calls = []
    monkeypatch.setattr(SolutionAgentsLoopRunner.__mro__[1], "prepare", lambda self, *a: calls.append(self.solutions(a[0], a[3])))

    class Broken:
        def get_spec(self, *a):
            raise OSError("quest unreachable")
    runner = SolutionAgentsLoopRunner(SubprocessConfig(working_dir="/tmp"), client_factory=lambda: Broken())
    runner.prepare("goal", "This run's quest is quest_abc123: add", "", "/tmp", "k")
    assert calls == [[]]
