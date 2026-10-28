"""A queued task's first run can go straight to a deep run, with no planner call.

``task_direct_deep`` (env ``QAR_TASK_DIRECT_DEEP``) makes the task executor pass the task's own
text as ``deep_goal``. The planner call is the largest prompt of a background run, so skipping it is
the saving; the deep worker reads what it needs and the goal check still judges the result.
Offline: a recording provider and a stub deep runner.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.adapters_events import Mode
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.runner.executor_followup import FollowUpMixin

from .conftest import StubRetrieval


class RecordingProvider:
    def __init__(self):
        self.schemas: List[Optional[str]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any], **kw) -> Dict[str, Any]:
        self.schemas.append(tool_schema.get("name"))
        if tool_schema.get("name") == "goal_verdict":
            return {"met": True}
        return {"action": "deep", "goal": "Do the work", "rationale": "deep", "user_intent": "act",
                "deep_subtasks": [{"goal": "Do the work", "brief": "do it"}]}

    def answer(self, messages, *, model, system=None, **kw) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class Runner:
    def run_goal(self, **kw) -> DeepResult:
        return DeepResult(met=True, output="done")


def run_background(**run_kwargs):
    provider = RecordingProvider()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=Runner(),
                        config=OrchestratorConfig())
    result = orch.run("Review the overdue goals and write a short note.", mode=Mode.BACKGROUND,
                      message_is_user_turn=False, **run_kwargs)
    return result, provider


def test_without_a_deep_goal_the_planner_runs():
    _, provider = run_background()
    assert "decide" in provider.schemas


def test_with_a_deep_goal_there_is_no_planner_call_and_the_goal_is_still_checked():
    result, provider = run_background(deep_goal="Complete this task: review the overdue goals")
    assert "decide" not in provider.schemas
    assert "goal_verdict" in provider.schemas
    assert result.kind == "deep" and result.deep_results[0].met


class Executor(FollowUpMixin):
    def __init__(self, direct: Any):
        self._orch = SimpleNamespace(cfg=SimpleNamespace(task_direct_deep=direct))


def test_the_executor_passes_the_task_text_as_the_goal_only_when_enabled():
    assert Executor(False)._direct_deep_extra("Do it", True) is None
    assert Executor("yes")._direct_deep_extra("Do it", True) is None  # only a real True counts
    assert Executor(True)._direct_deep_extra("Do it", False) is None  # brain cannot take a goal
    assert Executor(True)._direct_deep_extra("  ", True) is None
    extra = Executor(True)._direct_deep_extra("Do   it\n now " + "x" * 500, True)
    assert extra["deep_goal"].startswith("Complete this task: Do it now ")
    assert len(extra["deep_goal"]) <= len("Complete this task: ") + 300


def test_the_setting_defaults_to_off():
    assert OrchestratorConfig().task_direct_deep is False


def test_a_task_that_declares_its_environment_or_work_types_skips_the_planner():
    ex = Executor(False)
    assert ex._direct_deep_extra("Do it", True) is None
    assert ex._direct_deep_extra("Do it", True, {}) is None
    by_env = ex._direct_deep_extra("Do it", True, {"env_id": "env_1"})
    assert by_env == {"deep_goal": "Complete this task: Do it"}
    mixed = ex._direct_deep_extra("Do it", True, {"work_types": ["web_search", "quest_docs_ops"]})
    assert mixed["deep_goal"] == "Complete this task (work: web search, Quest docs operations): Do it"
    assert ex._direct_deep_extra("Do it", False, {"env_id": "e"}) is None  # old brain keeps planner
    off = Executor(False)
    off._orch.cfg.task_route_declared = False
    assert off._direct_deep_extra("Do it", True, {"env_id": "e"}) is None
