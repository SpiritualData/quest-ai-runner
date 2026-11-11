"""A runner that queues the work for another lane is told which model the PERSON named.

The queued task cannot tell that model from the automatic starting model it also receives as
``model``, so the named one travels in its own keyword, ``requested_model``, and only to a runner
that declares it. A request the worker cannot run, or none, sends nothing. Fully offline.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from hubofhubs.api import DeepResult
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

PLAN = {"action": "deep", "goal": "Do the work",
        "deep_subtasks": [{"goal": "Write the brief", "brief": "write it"}], "rationale": "deep"}


class HandOffRunner:
    """Declares ``requested_model``, like Quest's queued-task runner."""
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None, requested_model: Optional[str] = None) -> DeepResult:
        self.calls.append({"model": model, "requested_model": requested_model})
        return DeepResult(met=True, output="done")


class PlainRunner:
    """No ``requested_model`` keyword: must be called exactly as before."""
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({"model": model})
        return DeepResult(met=True, output="done")


def _run(runner, request):
    provider = ScriptedProvider(plans=[{**PLAN, "deep_model_request": request}], verdicts=[{"met": True}])
    Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                 deep_runner=runner, config=OrchestratorConfig()).run("do this")


def test_a_named_model_is_handed_to_the_queueing_runner():
    runner = HandOffRunner()
    _run(runner, "opus")
    assert [c["requested_model"] for c in runner.calls] == ["opus"]


def test_a_specific_version_is_handed_over_verbatim():
    runner = HandOffRunner()
    _run(runner, "claude-opus-4-8")
    assert [c["requested_model"] for c in runner.calls] == ["claude-opus-4-8"]


def test_nothing_named_hands_over_nothing():
    runner = HandOffRunner()
    _run(runner, None)
    assert [c["requested_model"] for c in runner.calls] == [None]


def test_a_model_the_worker_cannot_run_hands_over_nothing():
    runner = HandOffRunner()
    _run(runner, "gpt-5")
    assert [c["requested_model"] for c in runner.calls] == [None]


def test_a_runner_without_the_keyword_is_called_as_before():
    runner = PlainRunner()
    _run(runner, "opus")
    assert len(runner.calls) == 1 and "requested_model" not in runner.calls[0]


def test_work_deferred_from_an_answer_turn_keeps_the_named_model():
    """An "answer" with ``deferred_deep`` rebuilds the plan for the hand-off: the named model must ride it.

    Live (2026-11-10): the rebuilt plan carried the goal, brief and difficulty but not the request, so
    "use opus" was dropped whenever the planner answered first and handed the work off afterwards."""
    runner = HandOffRunner()
    plan = {"action": "answer", "rationale": "answer now, do the work after", "user_intent": "act",
            "deferred_deep": {"goal": "Write the script", "brief": "write it"}, "deep_model_request": "opus"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
                 deep_runner=runner, config=OrchestratorConfig()).run("answer, then use opus for the script")
    assert [c["requested_model"] for c in runner.calls] == ["opus"]


def test_the_persons_wording_around_the_name_is_dropped():
    """The planner copies the message verbatim: "best tier" and "the opus model" still name a model."""
    runner = HandOffRunner()
    _run(runner, "best tier")
    _run(runner, "the opus model")
    assert [c["requested_model"] for c in runner.calls] == ["best", "opus"]
