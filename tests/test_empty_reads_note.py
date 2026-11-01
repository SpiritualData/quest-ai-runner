"""Two read steps in a row that found nothing: the planner is told no further read will help."""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

NOTE = "no further read will return anything"


def read(path):
    return {"action": "read", "reads": [{"rel_path": path}], "rationale": "look"}


def test_planner_is_told_to_answer_after_two_empty_read_steps():
    provider = StubProvider([read("a.txt"), read("b.txt"), read("c.txt"),
                             {"action": "answer", "rationale": "nothing found"}])
    config = OrchestratorConfig(max_steps=6)
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=config)
    orch.run("What did I write down about the lease?")
    prompts = provider.plan_prompts
    assert all(NOTE not in p for p in prompts[:2])
    assert any(NOTE in p for p in prompts[2:])


def test_a_failed_escalation_that_changed_nothing_does_not_replace_the_answer():
    from types import SimpleNamespace as NS
    from quest_ai_runner.core.orchestrator_decide_tool import own_escalation_adds_nothing

    failed = NS(deep_results=[NS(met=False, error="mutation_codegen_failed", output="I couldn't turn that into a change",
                                 decision_id=None, usage_limited=False)], text="")
    assert own_escalation_adds_nothing(failed, NS(facts=[]), 0)
    filed = NS(deep_results=[NS(met=False, error="x", output="asked", decision_id="d1", usage_limited=False)], text="")
    assert not own_escalation_adds_nothing(filed, NS(facts=[]), 0)
    worked = NS(deep_results=[NS(met=False, error="late error", output="partly done", decision_id=None, usage_limited=False)], text="")
    assert not own_escalation_adds_nothing(worked, NS(facts=[NS(succeeded=True)]), 0)
    done = NS(deep_results=[NS(met=True, error=None, output="done", decision_id=None, usage_limited=False)], text="")
    assert not own_escalation_adds_nothing(done, NS(facts=[]), 0)
