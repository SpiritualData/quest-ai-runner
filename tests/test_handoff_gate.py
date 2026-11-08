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


# --- the hand-off steps: records read and one owned next-step write before the work is accepted as handed over

from quest_ai_runner.the_intelligence.mind.planner import handoff_gate
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec

RAN = []


def tool(name, mutates):
    def handler(args, ctx):
        RAN.append((name, args))
        return ToolResult(ok=True, text=f"{name} ok")
    return ToolSpec(name=name, description="d", mutates=mutates, handler=handler)


def tools():
    return ToolRegistry([tool("list_quest_goals", False), tool("list_quest_notes", False), tool("read_spec_records", False), tool("create_or_update_quest_goal", True)])


def deep():
    return {"action": "deep", "goal": "build the loop", "deep_brief": "b", "deep_target": "code_or_files", "user_intent": "act", "rationale": "hand over", "pending_read": []}


def read_step():
    return {"action": "tool", "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}], "user_intent": "act", "rationale": "read", "pending_read": []}


def write_step():
    return {"action": "tool", "tool_calls": [{"name": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title": "next step"}}], "user_intent": "act", "rationale": "write", "pending_read": []}


def run_with(decisions):
    RAN.clear()
    provider = Recording(decisions)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=OrchestratorConfig(overseer=False), tools=tools())
    orch.reach_verdict = lambda message: ORDER
    orch.run("make the loop happen", mode=Mode.LIVE, sink=RecordingSink(), quest_id="q1")
    return provider


def test_a_hand_over_with_nothing_read_or_written_is_sent_back_and_the_code_then_reads_the_records():
    provider = run_with([deep(), deep(), deep(), deep()])
    assert any("HAND-OFF NOT YET COMPLETE" in p and "owned next step" in p for p in provider.prompts)
    names = [name for name, _ in RAN]
    assert "list_quest_goals" in names and "list_quest_notes" in names and all(args.get("quest_id") == "q1" for _, args in RAN)      # the code read them, for the turn's quest
    assert "create_or_update_quest_goal" not in names                                                                                  # and wrote nothing itself


def test_a_hand_over_that_read_and_wrote_first_is_not_sent_back():
    provider = run_with([read_step(), write_step(), deep()])
    assert not any("HAND-OFF NOT YET COMPLETE" in p for p in provider.prompts)


def test_the_steps_are_facts_about_tool_receipts_not_words():
    reg = tools()
    assert handoff_gate.steps_missing([], reg) == ["read", "write"]
    read = {"locator": "tool:list_quest_goals", "text": "x"}
    assert handoff_gate.steps_missing([read], reg) == ["write"]
    assert handoff_gate.steps_missing([dict(read, intake=True)], reg) == ["read", "write"]          # a read the code made at intake is not the planner's read
    assert handoff_gate.steps_missing([read, {"locator": "tool:create_or_update_quest_goal", "text": "x"}], reg) == []
    assert [c["name"] for c in handoff_gate.owed_read_calls("q1", [read])] == ["list_quest_notes", "read_spec_records"]
    assert handoff_gate.owed_read_calls("", []) == []
