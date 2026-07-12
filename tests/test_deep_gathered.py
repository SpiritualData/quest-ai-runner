"""Tests that ``gathered`` is forwarded into ``context_preamble`` for the deep runner."""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval


class _PreambleCapturingRunner:
    """A DeepRunner that accepts context_preamble and records what it received."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({
            "goal": goal,
            "brief": brief,
            "model": model,
            "context_preamble": context_preamble,
        })
        return DeepResult(met=True, output="done")


def _orch(provider, retrieval, runner, **kw):
    return Orchestrator(
        retrieval=retrieval,
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=runner,
        **kw,
    )


def test_gathered_included_in_context_preamble_when_deep():
    """When the brain reads first then goes deep, gathered content reaches context_preamble."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "plan.md"}], "rationale": "read first"},
        {"action": "deep", "goal": "Do the work", "deep_brief": "do it", "rationale": "deep now"},
    ])
    retrieval = StubRetrieval({"plan.md": "GROUNDING the execution plan lives here"})
    runner = _PreambleCapturingRunner()

    res = _orch(provider, retrieval, runner).run("implement the plan")

    assert res.kind == "deep"
    assert runner.calls, "runner should have been called"
    preamble = runner.calls[0]["context_preamble"]
    assert preamble is not None, "context_preamble should be set when gathered is non-empty"
    assert "RELEVANT CONTENT FOUND BY THE BRAIN" in preamble
    assert "the execution plan lives here" in preamble


def test_rep_preamble_and_gathered_both_appear():
    """When rep_preamble is set AND gathered is non-empty, both appear in context_preamble."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "notes.md"}], "rationale": "read"},
        {"action": "deep", "goal": "Do work", "deep_brief": "do it", "rationale": "deep"},
    ])
    retrieval = StubRetrieval({"notes.md": "GROUNDING important note content"})
    runner = _PreambleCapturingRunner()

    rep_persona = "You are Alex's AI representative."
    res = _orch(provider, retrieval, runner).run("do work", rep_preamble=rep_persona)

    assert res.kind == "deep"
    preamble = runner.calls[0]["context_preamble"]
    assert preamble is not None
    # rep_preamble comes first
    assert preamble.index(rep_persona) < preamble.index("RELEVANT CONTENT FOUND BY THE BRAIN")
    assert "important note content" in preamble


def test_no_gathered_but_rep_preamble_still_forwarded():
    """When there is no gathered (direct deep), rep_preamble alone is still forwarded."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do work", "deep_brief": "do it", "rationale": "direct deep"},
    ])
    retrieval = StubRetrieval({})
    runner = _PreambleCapturingRunner()

    rep_persona = "You are a helpful AI."
    res = _orch(provider, retrieval, runner).run("do it", rep_preamble=rep_persona)

    assert res.kind == "deep"
    preamble = runner.calls[0]["context_preamble"]
    assert preamble == rep_persona


def test_no_gathered_no_rep_preamble_gives_no_preamble():
    """When there is nothing to forward, context_preamble is not set (remains None)."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do work", "deep_brief": "do it", "rationale": "direct deep"},
    ])
    retrieval = StubRetrieval({})
    runner = _PreambleCapturingRunner()

    res = _orch(provider, retrieval, runner).run("do it")

    assert res.kind == "deep"
    preamble = runner.calls[0]["context_preamble"]
    assert preamble is None
