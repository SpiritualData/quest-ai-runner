"""Three production findings on the Quest chat (eval run of 2026-11-08), offline.

1. A reply must not deny a change a deferred run's write receipt proves landed, even when the goal verifier judged the run "not met"
   (it added a goal where a workflow was asked for): the record marks that run failed, the receipt flag still proves the write.
2. A question for the person who is typing to us is asked in the reply; a decision-request is for someone not in the conversation.
3. The quest-data write-surface instruction names tools; a rung that declares it has none is not told to use them.
"""
from types import SimpleNamespace
from typing import Any, Dict

from hubofhubs.api import DeepResult
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import (
    QUEST_DATA_WRITE_SURFACE_INSTRUCTION, apply_quest_data_ladder_guard, rung_offers_quest_data_tools)
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn_messages as messages
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

from .conftest import StubProvider, StubRetrieval

DENIAL = "Nothing has been changed yet, and I have no tool here that writes to a quest."
RUN_ACCOUNT = "Added the goal Thursday office hour (weekly check-in) to the 1000 subscribers quest."


class DenyingProvider(StubProvider):
    """The pre-work answer and every rewrite deny the change; the denial judge answers from the prompt text."""

    def __init__(self):
        super().__init__(decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "describe", "user_intent": "act"}],
                         answer_text=DENIAL)

    def answer(self, messages, *, model, system=None) -> str:
        super().answer(messages, model=model, system=system)
        return DENIAL

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "landed_work_denial_verdict":
            return {"denies_landed_work": DENIAL in prompt}
        if tool_schema.get("name") == "goal_verdict":
            return {"met": False, "reason": "a goal was added where a workflow was asked for", "next_action": "", "claims_unexecuted": False}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


class OneResultRunner:
    def __init__(self, **fields):
        self.fields = fields

    def run_goal(self, *, goal, brief, model=None, max_turns=None):
        return DeepResult(**self.fields)


def run_turn(**fields):
    provider = DenyingProvider()
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.verify_claims = False
    cfg.answer_goal_max_iterations = 2
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=cfg,
                        deep_runner=OneResultRunner(**fields))
    return orch.run("Put the Thursday office hour on the quest as our weekly check-in.")


def test_reply_keeps_a_landed_write_the_verifier_called_not_met():
    res = run_turn(met=False, output=RUN_ACCOUNT, exhausted=True, observations_reported=True,
                   observations=["Added the goal Thursday office hour"], has_write_receipt=True)
    assert res.text == RUN_ACCOUNT, res.text


class RecordingSink:
    def __init__(self):
        self.filed = []

    def escalate(self, escalation, *a, **k):
        self.filed.append(escalation)
        return "dec_1"


def blocked_verdict():
    return {"blocker": "needs_person", "question": "Should it stay due today or move to the 15th?"}


def test_blocked_question_for_the_person_in_the_chat_is_asked_inline_not_filed():
    sink = RecordingSink()
    orch = SimpleNamespace(escalation=sink, _concise_decision_summary=lambda q: q)
    res = SimpleNamespace(output="", decision_id=None, observations=[], observations_reported=False, coverage=None)
    meta = {"live_user_turn": True, "asker_user_id": "u1", "participants": [{"user_id": "u1", "name": "Joshua"}]}
    filed = messages.file_blocked_ask(orch, None, meta, res, blocked_verdict(), question="Should it stay due today or move to the 15th?")
    assert filed is False
    assert sink.filed == []
    assert res.decision_id is None
    assert res.output == "Should it stay due today or move to the 15th?"


def test_blocked_question_with_nobody_attending_is_still_filed():
    sink = RecordingSink()
    orch = SimpleNamespace(escalation=sink, _concise_decision_summary=lambda q: q)
    res = SimpleNamespace(output="", decision_id=None, observations=[], observations_reported=False, coverage=None)
    meta = {"live_user_turn": False, "asker_user_id": "u1", "participants": [{"user_id": "u1", "name": "Joshua"}]}
    assert messages.file_blocked_ask(orch, None, meta, res, blocked_verdict(), question="Which source?") is True
    assert len(sink.filed) == 1


def test_blocked_question_for_a_teammate_is_filed_even_in_a_live_chat():
    sink = RecordingSink()
    orch = SimpleNamespace(escalation=sink, _concise_decision_summary=lambda q: q)
    res = SimpleNamespace(output="", decision_id=None, observations=[], observations_reported=False, coverage=None)
    meta = {"live_user_turn": True, "asker_user_id": "u1",
            "participants": [{"user_id": "u1", "name": "Joshua"}, {"user_id": "u2", "name": "Zee"}]}
    verdict = dict(blocked_verdict(), asked_of="member", member_ref="Zee")
    assert messages.file_blocked_ask(orch, None, meta, res, verdict, question="Can you send the file?") is True
    assert len(sink.filed) == 1


def test_instruction_is_left_out_of_the_brief_of_a_rung_that_declares_it_has_no_tools():
    texty = SimpleNamespace(write_surface="agent", offers_quest_data_tools=False)
    agent = SimpleNamespace(write_surface="agent")
    for deliberate in (True, False):
        assert apply_quest_data_ladder_guard([texty], "BRIEF", deliberate_choice=deliberate)[1] == "BRIEF"
        assert apply_quest_data_ladder_guard([agent], "BRIEF", deliberate_choice=deliberate)[1] == "BRIEF" + QUEST_DATA_WRITE_SURFACE_INSTRUCTION
    assert rung_offers_quest_data_tools(None) is True
