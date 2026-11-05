"""The critical-thinking area: its members answer offline by the strict definition, and the arm holds a request to its metric and hands over the rest."""
import pytest
from esoi.entry import Pending, targets

import quest_ai_runner
from quest_ai_runner.critical_thinking.area import MetricHolding, hold_case

AREA_IDS = ("critical-thinking", "iterate-by-spec", "spec-selection", "judge-a-decision", "guard-a-reply", "ground-in-intent",
            "detect-a-stalled-run", "draft-a-metric", "gate-an-autopilot-pass")


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
    assert strict_outcome(solution_id) in ("ok", "pending")


def test_the_members_that_need_no_key_return_a_value_offline():
    for solution_id in ("critical-thinking", "iterate-by-spec", "judge-a-decision", "guard-a-reply", "spec-selection"):
        assert strict_outcome(solution_id) == "ok"


def test_the_members_over_the_account_and_the_model_program_name_what_they_need():
    for solution_id, kind in (("ground-in-intent", "key"), ("draft-a-metric", "key"), ("gate-an-autopilot-pass", "key"),
                              ("detect-a-stalled-run", "install")):
        example = {sid: example for sid, level, k, example in targets(quest_ai_runner.assembler())}[solution_id]
        with pytest.raises(Pending) as caught:
            quest_ai_runner.run(solution_id, example, environ={})
        assert kind in {reason.kind for reason in caught.value.reasons} <= {kind, "needs"}


def test_a_deep_run_that_changed_something_keeps_its_reading_before_and_after():
    got = hold_case({"kind": "deep", "has_metric": True, "changed": True, "before": 0.4, "after": 0.6})
    assert [(row["before"], row["after"]) for row in got["rows"]] == [(0.4, 0.6)] and got["missing"] is False


def test_a_changed_run_with_no_metric_is_marked_missing_and_a_chat_turn_passes_through_nothing():
    assert hold_case({"kind": "deep", "has_metric": False, "changed": True, "before": None, "after": None})["missing"] is True
    assert hold_case({"kind": "chat", "has_metric": True, "changed": True, "before": 0.4, "after": 0.6})["rows"] == []


class Account:
    base_url, api_key, team_id = "http://quest.invalid", "key", ""

    def list_quest_goals(self, quest_id, **options):
        return []

    def get_intent_frame(self, quest_id):
        return {"quest_id": quest_id, "intent": "Ship the launch"}


class Stall:
    def review(self, elapsed, idle, tail):
        return {"stop": False, "reason": "still writing"}


class Draft:
    def draft(self, task_text, card, context_view=""):
        return {"propose": False, "reason": "thin evidence"}


class Gate:
    def gate(self, quest_id, now=""):
        return {"outcome": "skipped", "text": "daily budget spent"}


def test_the_area_hands_each_question_to_the_member_that_owns_it():
    provided = {"access-quest-operations": Account(), "detect-a-stalled-run": Stall(), "draft-a-metric": Draft(), "gate-an-autopilot-pass": Gate()}
    area = quest_ai_runner.run(MetricHolding, environ={}, provided=provided)
    assert area.reach_verdict('{"reach": "inside", "reason": "known"}')["reach"] == "inside"
    assert [url for url, label in area.links("See https://example.org/a for details.")] == ["https://example.org/a"]
    assert area.frame("q1")["intent"] == "Ship the launch"
    assert area.review("12 minutes", "9 minutes", "(no output)")["stop"] is False
    assert area.draft("Propose a value", {})["propose"] is False
    assert area.gate("q1")["outcome"] == "skipped"
    assert area.slice_for("Tidy the card store") is None
