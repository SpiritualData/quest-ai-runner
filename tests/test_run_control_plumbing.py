"""The run's control channel reaches a deep worker that declares it, and only that run's worker.

* the orchestrator hands ``run_control`` to a runner whose ``run_goal`` NAMES ``control``;
* a runner that only tolerates ``**kwargs`` is not given it (a claim nobody reads must not happen);
* the executor builds a control bound to its own task id: claims come from that task, seen is
  reported for that task, and a stop is read from that task.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters_values import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.run_control import ControlMessage, RunControl
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubProvider, StubRetrieval
from .test_runner import MockQuestClient


class ControlAwareRunner:
    def __init__(self) -> None:
        self.controls: List[Any] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, control=None) -> DeepResult:
        self.controls.append(control)
        return DeepResult(met=True, output="done")


class KwargsOnlyRunner:
    def __init__(self) -> None:
        self.kwargs: List[Dict[str, Any]] = []

    def run_goal(self, **kwargs) -> DeepResult:
        self.kwargs.append(kwargs)
        return DeepResult(met=True, output="done")


def deep_orchestrator(runner):
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": True, "reason": "done"},
    ])
    cfg = OrchestratorConfig(deep_goal_max_iterations=1)
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=runner, config=cfg)


def test_a_runner_that_declares_control_receives_this_runs_control():
    runner = ControlAwareRunner()
    control = RunControl(label="run-1")
    deep_orchestrator(runner).run("fix it", run_control=control)
    assert runner.controls == [control]


def test_no_control_is_passed_when_the_caller_gave_none():
    runner = ControlAwareRunner()
    deep_orchestrator(runner).run("fix it")
    assert runner.controls == [None]


def test_a_runner_that_only_tolerates_kwargs_is_not_given_control():
    runner = KwargsOnlyRunner()
    deep_orchestrator(runner).run("fix it", run_control=RunControl())
    assert "control" not in runner.kwargs[0]


class ControlClient(MockQuestClient):
    def __init__(self) -> None:
        super().__init__([])
        self.claimed_for: List[str] = []
        self.seen_calls: List[tuple] = []
        self.pending = [{"message_id": "m1", "text": "  also check the logs  "},
                        {"message_id": "m2", "text": ""}]
        self.cancelled = False
        self.pause = False

    def claim_task_messages(self, task_id):
        self.claimed_for.append(task_id)
        out, self.pending = self.pending, []
        return out

    def mark_task_messages_seen(self, task_id, message_ids, where="planner"):
        self.seen_calls.append((task_id, list(message_ids), where))
        return []

    def is_task_cancelled(self, task_id):
        return self.cancelled

    def is_task_pause_requested(self, task_id):
        return self.pause


def test_the_executor_builds_a_control_bound_to_its_own_task():
    client = ControlClient()
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    ex = TaskExecutor(client, Orchestrator(retrieval=StubRetrieval(), provider=provider,
                                           registry=ModelRegistry(provider)))
    control = ex._build_run_control("task-7")
    messages = control.claim_inputs()
    assert messages == [ControlMessage(id="m1", text="also check the logs")]   # blank text dropped
    assert client.claimed_for == ["task-7"]
    assert control.claim_inputs() == []                  # handed over once
    control.mark_seen(["m1"], "deep_worker")
    assert client.seen_calls == [("task-7", ["m1"], "deep_worker")]
    assert control.cancel_requested() is False and control.pause_requested() is False
    client.cancelled = True
    client.pause = True
    assert control.pause_requested() is True


def test_a_client_without_the_new_methods_still_gives_a_safe_control():
    client = MockQuestClient([])
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    ex = TaskExecutor(client, Orchestrator(retrieval=StubRetrieval(), provider=provider,
                                           registry=ModelRegistry(provider)))
    control = ex._build_run_control("task-8")
    assert control.claim_inputs() == []
    control.mark_seen(["x"], "deep_worker")             # no such client method: nothing raised
    assert control.pause_requested() is False
