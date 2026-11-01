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
