"""The records gate and the no-attachment gate, decided from the planner's structured fields (hard rule #3): no word of any reply is read.

* ``needs_records``: an answer that rests on the person's records, with nothing read this turn, goes back to the planner once.
* ``refers_to_attachment``: the message speaks of a photo or file and the turn holds no attachment object, so the reply says nothing
  arrived (a line the code writes) and nothing is queued or done on values nobody read.
"""
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decide_tool import DECIDE_TOOL
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import normalize_decision
from quest_ai_runner.the_intelligence.mind.planner.planner_prompt import NO_ATTACHMENT_LINE
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

from .conftest import StubProvider, StubRetrieval

FILES = {"log/entries.md": "GROUNDING the knee hurt at km 6, the physio banned running for 10 days, the race moved to 22 Nov"}


def orch(provider, retrieval):
    config = OrchestratorConfig()
    config.overseer = False
    return Orchestrator(retrieval=retrieval, provider=provider, registry=ModelRegistry(provider), config=config)


def answer(**fields):
    return {"action": "answer", "user_intent": "ask", "rationale": "answering", "pending_read": [], **fields}


READ = {"action": "read", "reads": [{"rel_path": "log/entries.md"}], "user_intent": "ask", "rationale": "read the log"}


def test_both_fields_are_in_the_schema_and_parse_only_from_true():
    props = DECIDE_TOOL["input_schema"]["properties"]
    assert props["needs_records"]["type"] == "boolean" and props["refers_to_attachment"]["type"] == "boolean"
    plan = normalize_decision({"action": "answer", "needs_records": True, "refers_to_attachment": True}, OrchestratorConfig())
    assert plan.needs_records is True and plan.refers_to_attachment is True
    plain = normalize_decision({"action": "answer", "needs_records": "yes"}, OrchestratorConfig())
    assert plain.needs_records is False and plain.refers_to_attachment is False


def test_an_answer_resting_on_records_with_nothing_read_goes_back_to_read_once():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([answer(needs_records=True), READ, answer(needs_records=True)])
    res = orch(provider, retrieval).run("I missed another run and want to quit.")
    assert retrieval.read_calls == ["log/entries.md"]
    assert res.kind == "answer"


def test_the_records_gate_fires_once_even_if_the_planner_keeps_answering():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([answer(needs_records=True)] * 4)
    res = orch(provider, retrieval).run("I missed another run and want to quit.")
    assert res.kind == "answer" and retrieval.read_calls == []


def test_an_answer_that_did_read_is_not_sent_back():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([READ, answer(needs_records=True)])
    orch(provider, retrieval).run("What did the log say?")
    assert retrieval.read_calls == ["log/entries.md"]


def test_an_answer_that_needs_no_records_is_not_sent_back():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([answer()])
    orch(provider, retrieval).run("What is a quest?")
    assert retrieval.read_calls == []


def test_a_message_that_refers_to_a_missing_attachment_is_answered_with_the_line_and_queues_nothing():
    retrieval = StubRetrieval(FILES)
    deep = {"action": "deep", "goal": "log the receipt", "user_intent": "act", "rationale": "log it", "pending_read": [],
            "refers_to_attachment": True}
    provider = StubProvider([deep, answer(refers_to_attachment=True)])
    res = orch(provider, retrieval).run("Here is a photo of my receipt. Please log the amount.")
    assert res.kind == "answer" and res.text.startswith(NO_ATTACHMENT_LINE)


def test_a_support_reply_is_sent_back_to_read_the_records_even_when_needs_records_was_not_set():
    retrieval = StubRetrieval(FILES)
    provider = StubProvider([answer(reply_shape="support"), READ, answer(reply_shape="support")])
    orch(provider, retrieval).run("I missed another run and want to quit.")
    assert retrieval.read_calls == ["log/entries.md"]
