"""The nervous-system area: its members answer offline by the strict definition, and the overseer reads a digest and hands a step to a person."""
import pytest
from esoi import Pending, targets

import quest_ai_runner
from quest_ai_runner.the_intelligence.nervous_system.interface import RunWatching
from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from tests.conftest import StubProvider

AREA_IDS = ("nervous-system", "escalate-to-a-human")


def strict_outcome(solution_id):
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}[solution_id]
    try:
        quest_ai_runner.run(solution_id, example, environ={})
    except Pending as pending:
        assert {reason.kind for reason in pending.reasons} <= {"key", "install", "needs"}, pending.lines
        return "pending"
    return "ok"


@pytest.mark.parametrize("solution_id", AREA_IDS)
def test_each_id_of_the_area_answers_offline(solution_id):
    assert strict_outcome(solution_id) == "pending"


def test_the_overseer_pends_on_the_model_program_and_the_escalation_on_the_account_key():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("nervous-system", "delete the old backup folder", environ={})
    assert {reason.kind for reason in caught.value.reasons} == {"install"}
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("escalate-to-a-human", Escalation(summary="Approve the note"), environ={})
    assert "key" in {reason.kind for reason in caught.value.reasons}


class ScriptedModel(StubProvider):
    """A scripted model whose structured answer is the overseer's signal."""

    supports_web_search = False

    def web_search(self, *args, **kwargs):
        raise NotImplementedError

    def plan(self, prompt, *, model, tool_schema, layers=None):
        self.plan_calls += 1
        return {"signal": "escalate_human", "reason": "Do you want me to delete the backup folder?", "hint": ""}


class Sink:
    def __init__(self):
        self.filed = []

    def escalate(self, escalation):
        self.filed.append(escalation)
        return "decision-1"


def test_the_overseer_sends_one_risk_signal_and_hands_a_step_to_a_person():
    sink = Sink()
    area = quest_ai_runner.run(RunWatching, environ={}, provided={"vendor-model-caller": ScriptedModel([]), "escalate-to-a-human": sink})
    digest = area.digest(user_message="delete the old backup folder", goal_condition="keep the backups", step=1, max_steps=5)
    assert "delete the old backup folder" in digest
    signal = area.watch(digest)
    assert signal.severity == "high" and signal.degraded is False
    verdict = area.oversee(ScriptedModel([]), "m", digest)       # the five-way verdict QAR's loop acts on, the hub's RunOverseer
    assert verdict.signal == "escalate_human" and verdict.reason == signal.reason
    assert area.escalate(Escalation(summary=signal.reason)) == "decision-1" and sink.filed[0].summary == signal.reason


def test_a_model_that_fails_degrades_to_proceed():
    class Down(ScriptedModel):
        def plan(self, prompt, *, model, tool_schema, layers=None):
            raise RuntimeError("provider down")

    area = quest_ai_runner.run(RunWatching, environ={}, provided={"vendor-model-caller": Down([]), "escalate-to-a-human": Sink()})
    signal = area.watch("digest")
    assert signal.severity == "none" and signal.degraded is True
    assert area.oversee(Down([]), "m", "digest").signal == "proceed"
