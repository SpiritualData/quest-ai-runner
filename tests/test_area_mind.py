"""The Mind area: its members answer offline by the strict definition, and the composition arm runs over them."""
import pytest
from esoi.entry import Pending

import quest_ai_runner
from quest_ai_runner.area_mind import StepReasoning
from quest_ai_runner.core.interim_policy import EXAMPLE_STATE
from quest_ai_runner.loop_wiring import GoalAnswer
from tests.conftest import StubProvider

AREA_IDS = ("the-mind", "vendor-model-caller", "model-tier-chooser", "assemble-a-prompt", "answer-a-turn", "answer-end-to-end",
            "replay-known-operations", "core-ml-tasks", "route-a-message", "reply-in-steps", "show-the-ais-work")




def strict_outcome(solution_id):
    from esoi.entry import targets
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}[solution_id]
    try:
        quest_ai_runner.run(solution_id, example, environ={})
    except Pending as pending:
        kinds = {reason.kind for reason in pending.reasons}
        assert kinds <= {"key", "install", "needs"}, (solution_id, pending.lines)
        return "pending"
    return "ok"


@pytest.mark.parametrize("solution_id", AREA_IDS)
def test_each_id_of_the_area_answers_offline(solution_id):
    assert strict_outcome(solution_id) in ("ok", "pending")


def test_the_members_that_need_no_model_return_a_value_offline():
    for solution_id in ("model-tier-chooser", "assemble-a-prompt", "replay-known-operations", "reply-in-steps", "show-the-ais-work"):
        assert strict_outcome(solution_id) == "ok"


def test_the_area_pends_on_the_model_program_only():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("the-mind", {"request": "What should I work on first this week?"}, environ={})
    assert {reason.kind for reason in caught.value.reasons} == {"install"}


class Recorder:
    """An emit target that keeps events."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class ScriptedModel(StubProvider):
    """A scripted model that cannot search the web."""

    supports_web_search = False

    def web_search(self, *args, **kwargs):
        raise NotImplementedError


def scripted(*decisions):
    return ScriptedModel(list(decisions) or [{"action": "answer", "rationale": "known", "model_tier": "sonnet"}], answer_text="Start with the outline.")


def test_think_runs_one_turn_of_the_loop_and_returns_the_minds_mapping():
    provider = scripted()
    got = quest_ai_runner.run("the-mind", {"request": "What should I work on first this week?"}, environ={}, provided={"vendor-model-caller": provider})
    assert "Start with the outline." in got["text"] and got["pieces"] == [] and got["results"] == [] and got["kind"] == "answer"
    assert provider.answer_calls >= 1


def test_the_model_asked_at_a_tier_is_model_plumbings_ask_over_qars_vendor_caller_and_tier_chooser():
    provider = scripted()
    text = quest_ai_runner.run("model-plumbing", {"prompt": "Reply with the single word: ok", "tier": "cheap"}, environ={},
                               provided={"vendor-model-caller": provider})
    assert "Start with the outline." in text and provider.answer_calls == 1


def test_a_step_starts_on_the_model_the_tier_chooser_measured_best_for_the_tier():
    area = quest_ai_runner.run(StepReasoning, environ={}, provided={"vendor-model-caller": scripted()})
    assert area.starts_on("fast") and area.starts_on("not-a-tier") == "not-a-tier"


def test_the_composition_hands_each_part_of_a_step_to_its_member():
    provider = scripted({"action": "answer", "rationale": "known", "model_tier": "sonnet"})
    area = quest_ai_runner.run(StepReasoning, environ={}, provided={"vendor-model-caller": provider})
    assert "PLANNER" in area.step_prompt("What should I work on first this week?")
    assert area.classify("What should I work on first this week?") == "answer"
    assert area.interim(EXAMPLE_STATE).send is True
    assert list(area.replayable("Add a note that the launch moves to Friday")) == []
    sink = Recorder()
    assert area.trail(sink, {"work_trace": {"audience": "owner", "trace_id": "t1"}}) is not None
    assert area.trail(sink, None) is None


def test_a_turn_and_the_shipped_path_return_the_loop_whole():
    provider = scripted()
    area = quest_ai_runner.run(StepReasoning, environ={}, provided={"vendor-model-caller": provider})
    for answer in (area.turn("What should I work on first this week?"), area.end_to_end("What should I work on first this week?")):
        assert isinstance(answer, GoalAnswer) and answer.kind == "answer" and "Start with the outline." in answer.text
