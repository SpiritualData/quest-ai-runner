"""Offline tests for the cheap information turn: a plain question answered from records spends few model calls.

Covers: the structured test for an information turn; the explanation panel built without its prose call; the goal-verifier loop skipped
(and kept on the other arm); tool arguments the tool does not have dropped for a read and refused for a write; the keyword and turn-history
context stores skipping their model filter on the cheap path.
"""
from typing import Any, Dict, List

from hubofhubs.api import OVERSEE_TOOL, Mode, with_retrieve_depth

from .conftest import StubProvider, StubRetrieval
from .test_answer_explanation import RecordingSink, SAMPLE_EXPLANATION, ExplainingProvider, orch
from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.turn_context_store import TurnContextStore
from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import is_information_turn
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolSpec


def test_an_information_turn_is_decided_from_the_planners_structured_fields():
    plain = PlanDecision(action="answer")
    assert is_information_turn("ask", plain)
    assert not is_information_turn("act", plain)
    assert not is_information_turn(None, plain)
    assert not is_information_turn("ask", PlanDecision(action="deep"))
    assert not is_information_turn("ask", PlanDecision(action="answer", reply_shape="support"))
    assert is_information_turn("ask", PlanDecision(action="answer", reply_shape="one_step"))
    assert not is_information_turn("ask", PlanDecision(action="answer", deferred_deep="do the thing"))


def information_provider():
    return ExplainingProvider(
        decisions=[{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look", "user_intent": "ask"},
                   {"action": "answer", "rationale": "have it", "user_intent": "ask"}],
        explanation=SAMPLE_EXPLANATION)


def test_an_information_turn_builds_its_explanation_panel_without_the_prose_call():
    provider = information_provider()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1))
    res = o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=RecordingSink())
    assert provider.explain_prompts == []                    # no model call for the prose
    assert res.explanation and res.explanation.get("used")   # the recorded facts are still there


def test_the_other_arm_still_writes_the_prose():
    provider = information_provider()
    o = orch(provider, files={"README.md": "Metrics are counted weekly."},
             config=OrchestratorConfig(explain_answer=True, verify_claims=False, answer_goal_max_iterations=1, cheap_information_turns=False))
    o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=RecordingSink())
    assert len(provider.explain_prompts) == 1


def test_the_goal_verifier_is_skipped_for_an_information_turn_and_runs_on_the_other_arm(caplog):
    """The verifier's own log line (one per attempt, ``Goal verifier did not run`` or ``Answer verdict``) is the witness."""
    import logging
    caplog.set_level(logging.INFO)
    decisions = [{"action": "answer", "rationale": "have it", "user_intent": "ask"}]
    seen = {}
    for cheap in (True, False):
        caplog.clear()
        o = orch(StubProvider(decisions=decisions), config=OrchestratorConfig(
            verify_claims=True, answer_goal_max_iterations=2, cheap_information_turns=cheap))
        o.run("what is our top priority", mode=Mode.LIVE, sink=RecordingSink())
        seen[cheap] = sum(1 for r in caplog.records if "Goal verifier did not run" in r.getMessage() or "Answer verdict" in r.getMessage())
    assert seen[True] == 0 and seen[False] >= 1


def make_tool(mutates: bool, seen: List[Dict[str, Any]]):
    return ToolSpec(name="list_things", description="lists", handler=lambda args, ctx: (seen.append(dict(args)) or "ok"),
                    when_to_use="x", when_not_to_use="y", mutates=mutates,
                    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}})


def test_a_read_tool_drops_an_argument_it_does_not_have_and_still_reads():
    seen: List[Dict[str, Any]] = []
    result = ToolRegistry([make_tool(False, seen)]).invoke("list_things", {"limit": 3, "open_only": True}, ToolContext())
    assert result.ok and seen == [{"limit": 3}]
    assert "open_only" in result.text and "Ignored" in result.text


def test_a_tool_that_changes_something_still_refuses_an_unknown_argument():
    seen: List[Dict[str, Any]] = []
    result = ToolRegistry([make_tool(True, seen)]).invoke("list_things", {"limit": 3, "open_only": True}, ToolContext())
    assert not result.ok and seen == [] and "unknown argument" in result.text


class FilterCounting:
    def __init__(self):
        self.calls = 0

    def answer(self, messages, *, model=None, system=None):
        self.calls += 1
        return '{"cards": []}'

    def list_models(self):
        return ["m"]


def test_the_turn_history_store_skips_its_model_filter_on_the_cheap_path_and_when_there_is_no_choice(tmp_path):
    provider = FilterCounting()
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"), provider=provider, model="m", max_older=2)
    for i in range(6):
        store.record(f"question about priority topic{i}", {"response": f"answer{i}"})
    store.assemble("priority topic", meta=with_retrieve_depth(None))
    assert provider.calls == 0                                 # the cheap path never filters
    store.assemble("priority topic")
    assert provider.calls >= 1                                 # six matches, two kept: the filter has a choice to make
    few = TurnContextStore(turns_dir=str(tmp_path / "few"), provider=FilterCounting(), model="m", max_older=4)
    few.record("question about priority", {"response": "a"})
    few.assemble("priority")
    assert few._provider.calls == 0                            # one match, four kept: nothing to choose between


class OverseerCounting(StubProvider):
    def __init__(self, decisions):
        super().__init__(decisions=decisions)
        self.consults = 0

    def plan(self, prompt, *, model, tool_schema):
        if (tool_schema or {}).get("name") == OVERSEE_TOOL["name"]:
            self.consults += 1
            return {"signal": "proceed", "reason": "ok"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def test_the_in_loop_overseer_is_skipped_on_a_read_only_information_turn_and_runs_on_the_other_arm():
    reads = [{"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": f"look {i}", "user_intent": "ask", "goal": f"g{i}"} for i in range(3)]
    decisions = reads + [{"action": "answer", "rationale": "have it", "user_intent": "ask"}]
    seen = {}
    for cheap in (True, False):
        provider = OverseerCounting(decisions)
        o = orch(provider, files={"README.md": "Metrics are counted weekly."}, config=OrchestratorConfig(
            overseer=True, overseer_poll_timeout_seconds=5.0, verify_claims=False, answer_goal_max_iterations=1, cheap_information_turns=cheap,
            overseer_gate_min_consecutive_reads=0, overseer_gate_spend_fraction=0.0))
        o.run("what does the readme say about metrics", mode=Mode.LIVE, sink=RecordingSink())
        seen[cheap] = provider.consults
    assert seen[True] == 0 and seen[False] >= 1
