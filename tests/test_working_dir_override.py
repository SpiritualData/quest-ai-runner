"""Per-task working directory: the executor resolves a task's goal/quest id through the
configured ``quest_folder_map`` and, when mapped, the deep run starts THERE for this run only —
falling back to the deep-runner's configured global working_dir otherwise. See
``quest_autopilot_design.md``'s execution-environment section ("one quest, one folder, one env").

All offline: a capturing deep runner (records every kwarg ``run_goal`` was called with) plus the
real ``Orchestrator``/``TaskExecutor``/``SubprocessGoalRunner``. No network, no subprocess spawn
(``SubprocessGoalRunner`` tests intercept ``subprocess.Popen``).
"""
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubDeepRunner, StubProvider, StubRetrieval
from .test_runner import MockQuestClient


class CapturingDeepRunner:
    """A DeepRunner whose ``run_goal`` accepts (and records) a ``working_dir`` override."""

    def __init__(self, met: bool = True, output: str = "deep done"):
        self._met = met
        self._output = output
        self.calls = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None,
                 context_preamble=None, working_dir=None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "working_dir": working_dir})
        return DeepResult(met=self._met, output=self._output)


def _deep_task_provider():
    return StubProvider(decisions=[
        {"action": "deep", "goal": "do the work", "deep_brief": "x", "rationale": "work"},
        {"met": True, "reason": "did it"},
    ])


def _brain(deep_runner):
    provider = _deep_task_provider()
    return Orchestrator(retrieval=StubRetrieval({"README.md": "fact"}),
                        provider=provider, registry=ModelRegistry(provider),
                        deep_runner=deep_runner)


# --- TaskExecutor._resolve_working_dir (pure resolution logic) --------------------------------

def test_resolve_working_dir_prefers_goal_id_over_quest_id():
    ex = TaskExecutor(MockQuestClient([]), _brain(CapturingDeepRunner()),
                      quest_folder_map={"goal1": "/goals/goal1", "quest1": "/quests/quest1"})
    assert ex._resolve_working_dir("goal1", "quest1") == "/goals/goal1"


def test_resolve_working_dir_uses_quest_id_key_only_when_goal_id_is_absent():
    """Mirrors the poller's own ``_quest_folder_for`` precedence: the lookup KEY is goal_id when
    present, else quest_id -- it is a single-key choice, not a fallback chain across both ids (a
    goal_id that IS present but unmapped does not fall through to a quest_id mapping)."""
    ex = TaskExecutor(MockQuestClient([]), _brain(CapturingDeepRunner()),
                      quest_folder_map={"quest1": "/quests/quest1"})
    assert ex._resolve_working_dir(None, "quest1") == "/quests/quest1"
    assert ex._resolve_working_dir("goal_unmapped", "quest1") is None


def test_resolve_working_dir_none_when_nothing_mapped():
    ex = TaskExecutor(MockQuestClient([]), _brain(CapturingDeepRunner()),
                      quest_folder_map={"other": "/other"})
    assert ex._resolve_working_dir("goal1", "quest1") is None


def test_resolve_working_dir_none_when_map_unconfigured():
    ex = TaskExecutor(MockQuestClient([]), _brain(CapturingDeepRunner()))
    assert ex._resolve_working_dir("goal1", "quest1") is None


def test_resolve_working_dir_none_when_no_ids_present():
    ex = TaskExecutor(MockQuestClient([]), _brain(CapturingDeepRunner()),
                      quest_folder_map={"goal1": "/goals/goal1"})
    assert ex._resolve_working_dir(None, None) is None


# --- end-to-end: execute() threads the resolved folder into the deep run -----------------------

def test_executor_threads_mapped_goal_folder_into_deep_run():
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(deep),
                      quest_folder_map={"goal_42": "/hq/stories/some_quest"})
    ex.execute({"id": "t1", "text": "do work", "goal_id": "goal_42"})
    assert deep.calls and deep.calls[0]["working_dir"] == "/hq/stories/some_quest"


def test_executor_falls_back_to_runners_default_when_goal_not_mapped():
    """No mapping -> working_dir=None is forwarded, so the deep runner's OWN configured global
    working_dir applies (see SubprocessGoalRunner: working_dir=None means 'use cfg.working_dir')."""
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(deep),
                      quest_folder_map={"some_other_goal": "/somewhere"})
    ex.execute({"id": "t2", "text": "do work", "goal_id": "goal_unmapped"})
    assert deep.calls and deep.calls[0]["working_dir"] is None


def test_executor_uses_quest_id_mapping_when_no_goal_id_present():
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(deep),
                      quest_folder_map={"quest_99": "/hq/stories/quest99_folder"})
    ex.execute({"id": "t3", "text": "do work", "quest_id": "quest_99"})
    assert deep.calls and deep.calls[0]["working_dir"] == "/hq/stories/quest99_folder"


def test_executor_no_quest_folder_map_at_all_is_unchanged_behavior():
    deep = CapturingDeepRunner()
    ex = TaskExecutor(MockQuestClient([]), _brain(deep))  # quest_folder_map omitted entirely
    ex.execute({"id": "t4", "text": "do work", "goal_id": "g1", "quest_id": "q1"})
    assert deep.calls and deep.calls[0]["working_dir"] is None


def test_older_deep_runner_without_working_dir_kwarg_is_untouched():
    """A DeepRunner whose run_goal does NOT accept working_dir (the conftest stub) keeps working
    even when a mapped folder exists — the orchestrator simply never forwards it."""
    deep = StubDeepRunner(met=True, output="ok")
    ex = TaskExecutor(MockQuestClient([]), _brain(deep),
                      quest_folder_map={"goal_1": "/mapped/folder"})
    out = ex.execute({"id": "t5", "text": "do work", "goal_id": "goal_1"})
    assert out.status == "done"
    assert deep.calls and "working_dir" not in deep.calls[0]


# --- SubprocessGoalRunner: the actual per-call cwd override + fallback --------------------------

def test_subprocess_runner_uses_working_dir_override_for_cwd(monkeypatch):
    import subprocess as _sp

    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    captured = {}

    class _MockPopen:
        returncode = 0
        stdin = None

        def communicate(self, input=None, timeout=None):
            return (b"did it", b"")

    def _fake_popen(cmd, **kw):
        captured["cwd"] = kw.get("cwd")
        return _MockPopen()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/global/default"))
    res = runner.run_goal(goal="g", brief="b", max_turns=2, working_dir="/quest/synced/folder")
    assert res.met is True
    assert captured["cwd"] == "/quest/synced/folder"


def test_subprocess_runner_falls_back_to_configured_working_dir_when_override_omitted(monkeypatch):
    import subprocess as _sp

    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    captured = {}

    class _MockPopen:
        returncode = 0
        stdin = None

        def communicate(self, input=None, timeout=None):
            return (b"did it", b"")

    def _fake_popen(cmd, **kw):
        captured["cwd"] = kw.get("cwd")
        return _MockPopen()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/global/default"))
    res = runner.run_goal(goal="g", brief="b", max_turns=2)  # no working_dir override
    assert res.met is True
    assert captured["cwd"] == "/global/default"


def test_goal_runner_wrapper_forwards_working_dir_only_when_accepted():
    """GoalRunner.run() mirrors the context_preamble opt-in discipline for working_dir: forwarded
    to a runner that accepts it, omitted for one that doesn't (StubDeepRunner)."""
    from quest_ai_runner.core.goal_runner import GoalRunner

    class AcceptingRunner:
        def __init__(self):
            self.calls = []

        def run_goal(self, *, goal, brief, model=None, max_turns=None, working_dir=None):
            self.calls.append(working_dir)
            return DeepResult(met=True, output="ok")

    accepting = AcceptingRunner()
    gr = GoalRunner(accepting)
    gr.run(goal="g", brief="b", working_dir="/synced/folder")
    assert accepting.calls == ["/synced/folder"]

    old = StubDeepRunner(met=True, output="ok")
    gr_old = GoalRunner(old)
    res = gr_old.run(goal="g", brief="b", working_dir="/synced/folder")
    assert res.met is True
    assert "working_dir" not in old.calls[0]
