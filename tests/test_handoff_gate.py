"""Offline tests: an order the reach check settled as outside, covered by an attached environment and asking for a change is handed off, not answered as a question."""
from hubofhubs.api import Mode

from .conftest import StubProvider, StubRetrieval
from .test_answer_explanation import RecordingSink
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.handoff_gate import must_hand_off
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

ORDER = {"reach": "outside", "covered_by": "build-environment", "asks_change": True}


def test_the_gate_reads_only_the_judges_fields():
    assert must_hand_off(ORDER)
    assert not must_hand_off(dict(ORDER, asks_change=False)) and not must_hand_off(dict(ORDER, covered_by=None))
    assert not must_hand_off(dict(ORDER, reach="inside")) and not must_hand_off(None) and not must_hand_off({"reach": "outside"})


class Recording(StubProvider):
    def __init__(self, decisions):
        super().__init__(decisions=decisions)
        self.prompts = []

    def plan(self, prompt, *, model, tool_schema):
        self.prompts.append(prompt)
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def run(verdict):
    answer = {"action": "answer", "user_intent": "ask", "rationale": "I have no material", "reply_shape": "default"}
    provider = Recording([answer, dict(answer, rationale="handing it over")])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=OrchestratorConfig(overseer=False))
    orch.reach_verdict = lambda message: verdict
    res = orch.run("what is missing for the loop, and make sure it happens", mode=Mode.LIVE, sink=RecordingSink())
    return provider, res


def test_a_covered_order_that_the_planner_answered_as_a_question_is_sent_back_once_to_hand_off():
    provider, res = run(ORDER)
    assert sum("HAND-OFF REQUIRED" in p for p in provider.prompts) >= 1 and res.steps >= 2
    provider, res = run(dict(ORDER, asks_change=False))
    assert not any("HAND-OFF REQUIRED" in p for p in provider.prompts) and res.steps == 1
