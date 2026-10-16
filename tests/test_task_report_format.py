"""A finished background task's chat report has a fixed two-part shape.

``synthesize_task_report`` is what the executor posts into the chat that asked for the work. It must
tell the person what was done and give numbered steps they can follow to test it, add "Still open"
only when something is unfinished, forbid em dashes, and still hand the model only the run's real
RESULT to ground on. The interactive after-deep reply keeps its own prompt.
"""
from quest_ai_runner.core.auxiliary_prompts import (
    SYNTHESIZE_AFTER_DEEP_PROMPT,
    SYNTHESIZE_TASK_REPORT_PROMPT,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval


def make_orchestrator(provider: StubProvider) -> Orchestrator:
    config = OrchestratorConfig()
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=config)


def test_task_report_prompt_has_both_parts_and_forbids_em_dashes():
    assert "What was done:" in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "How to test, step by step:" in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "Still open:" in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "ONLY when" in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "Never use an em dash" in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "—" not in SYNTHESIZE_TASK_REPORT_PROMPT
    assert "Numbered steps" in SYNTHESIZE_TASK_REPORT_PROMPT


def test_synthesize_task_report_sends_the_two_part_prompt_with_the_real_result():
    provider = StubProvider([], answer_text="REPORT")
    orch = make_orchestrator(provider)
    out = orch.synthesize_task_report("Create a goal for the grant outline",
                                      "Created goal 'Grant outline' on the Funding quest.")
    assert out.startswith("REPORT")
    sent = "\n".join(str(m["content"]) for m in provider.last_answer_messages)
    assert "How to test, step by step:" in sent
    assert "Create a goal for the grant outline" in sent
    assert "Created goal 'Grant outline' on the Funding quest." in sent
    # The interactive after-deep prompt is not what a background task report uses.
    assert SYNTHESIZE_AFTER_DEEP_PROMPT.split("=== USER REQUEST")[0] not in sent


def test_after_deep_reply_keeps_its_own_prompt():
    provider = StubProvider([], answer_text="REPLY")
    orch = make_orchestrator(provider)
    orch._synthesize_after_deep("do x", prior_answer="", deep_output="did x",
                                transcript="", model=orch.registry.resolve_tier("balanced"))
    sent = "\n".join(str(m["content"]) for m in provider.last_answer_messages)
    assert "How to test, step by step:" not in sent
