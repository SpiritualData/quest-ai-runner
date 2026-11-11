"""A message that orders a text ALSO saved in the person's records (research, then a note on the quest) is not answered with the text alone: the request
analyser's structured ``saves_to_records`` report keeps the planner's ``draft_in_chat`` from standing for the write, so the order not carried out is handed
back to plan the tool step (hard rule #3: a structured report, never a word of the message or the reply)."""
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import parse_goal_condition_reply, take_status_flags
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec

from .conftest import StubProvider, StubRetrieval

RAN = []


def add_note_tool():
    def handler(args, ctx):
        RAN.append(args)
        return ToolResult(ok=True, text="note added")
    return ToolRegistry([ToolSpec(name="add_quest_note", description="d", mutates=True, handler=handler)])


class AnalyserProvider(StubProvider):
    def __init__(self, analyser, decisions):
        super().__init__(decisions)
        self.analyser = analyser

    def answer(self, messages, *, model=None, system=None):
        if "request analyser" in "\n".join(str(m.get("content")) for m in messages) + (system or ""):
            return self.analyser
        return super().answer(messages, model=model, system=system)


DRAFT = {"action": "answer", "user_intent": "act", "deep_target": "draft_in_chat", "rationale": "write it", "pending_read": []}
WRITE = {"action": "tool", "tool_calls": [{"name": "add_quest_note", "args": {"quest_id": "q1", "text": "table and steps"}}], "user_intent": "act", "rationale": "save it", "pending_read": []}
DONE = {"action": "answer", "user_intent": "act", "rationale": "done", "pending_read": []}
MESSAGE = "research typical costs for retiling a small bathroom floor, then add a note to this quest with a short budget table and the three steps"


def run(analyser, decisions):
    RAN.clear()
    provider = AnalyserProvider(analyser, decisions)
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=config, tools=add_note_tool())
    orch.run(MESSAGE, quest_id="q1")
    return provider


def test_the_analyser_reports_saves_to_records_as_a_field_not_a_filter():
    _, constraints = parse_goal_condition_reply('Research, then a note.\n{"saves_to_records": true, "writes_text": true}')
    flags, rest = take_status_flags(constraints)
    assert flags["saves_to_records"] is True and flags["writes_text"] is True and rest is None


def test_a_text_that_is_also_ordered_saved_is_not_left_as_the_reply_and_the_note_is_written():
    provider = run('Research, then a note.\n{"saves_to_records": true}', [DRAFT, WRITE, DONE])
    assert RAN == [{"quest_id": "q1", "text": "table and steps"}]
    assert any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts)


def test_a_text_with_nothing_ordered_saved_stays_the_reply_and_writes_nothing():
    provider = run('A post.\n{"writes_text": true}', [DRAFT, WRITE, DONE])
    assert RAN == [] and not any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts)


ASK_READ = {"action": "answer", "user_intent": "ask", "rationale": "answer from what I know", "pending_read": []}
EXACT = "Look into what research says about how many minutes of daily singing it takes to notice a better mood, then add a short note to this quest with what you found."


def test_look_into_x_then_add_a_note_is_an_order_even_when_the_planner_read_it_as_a_question():
    RAN.clear()
    provider = AnalyserProvider('Research, then a note.\n{"saves_to_records": true}', [ASK_READ, WRITE, DONE])
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=config, tools=add_note_tool())
    orch.run(EXACT, quest_id="q1")
    assert RAN, "the note was written"
    assert any("ORDER NOT CARRIED OUT" in p for p in provider.plan_prompts)


def test_a_plain_question_with_no_save_ordered_stays_a_question():
    RAN.clear()
    provider = AnalyserProvider("What research says about singing.\n{}", [ASK_READ, WRITE, DONE])
    config = OrchestratorConfig()
    config.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=config, tools=add_note_tool())
    orch.run("What does research say about how many minutes of daily singing improve mood?", quest_id="q1")
    assert RAN == []
