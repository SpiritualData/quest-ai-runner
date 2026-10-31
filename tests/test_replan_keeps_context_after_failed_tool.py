"""A re-plan after a tool call that did not land keeps the full context; after a read it still abbreviates it."""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

CONTEXT = "CONTEXT-MARKER: quest q_home is the bathroom renovation"


def replan_prompt(gathered):
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "done"}])
    cfg = OrchestratorConfig(planner_abbreviate_repeat_context=True)
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg)
    orch._plan("add a goal", "", CONTEXT, gathered, step=1)
    return provider.plan_prompts[0]


def test_failed_tool_call_keeps_the_context_on_the_replan():
    failed = [{"kind": "query", "locator": "tool:create_quest_goal", "tool_status": "failed",
               "text": "TOOL create_quest_goal failed: No quest was named."}]
    assert CONTEXT in replan_prompt(failed)


def test_refused_tool_call_keeps_the_context_on_the_replan():
    refused = [{"kind": "query", "locator": "tool:create_quest_goal", "tool_status": "refused", "text": "TOOL refused"}]
    assert CONTEXT in replan_prompt(refused)


def test_applied_tool_call_still_abbreviates_the_context():
    applied = [{"kind": "query", "locator": "tool:create_quest_goal", "tool_status": "applied", "text": "TOOL applied"}]
    prompt = replan_prompt(applied)
    assert CONTEXT not in prompt
    assert "unchanged since step 1" in prompt
