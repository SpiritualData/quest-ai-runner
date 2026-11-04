"""The hold-the-intents area: its members answer offline by the strict definition, and the area reads its stores for one request."""
import pytest
from esoi.entry import Pending, targets

import quest_ai_runner
from quest_ai_runner.area_hold_the_intents import IntentHolding

AREA_IDS = ("hold-the-intents", "remember-conversations", "recall-recent-context", "guide-by-feedback", "store-feedback",
            "persist-predictions", "select-a-rep", "guide-toward-the-long-term-goal")


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


class Recent:
    def load(self, scope_keys):
        return [{"id": "card-1", "scope": scope_keys}]

    def record(self, scope_keys, cards, user_text):
        return None


class Guidance:
    def select(self, user_message="", **options):
        return [f"applies to: {user_message} ({options['limit']})"]

    def list(self):
        return []

    def read(self, card_id):
        return None


class Ledger:
    def open_items(self, card_id):
        return ["owed"]

    def standing_rules(self, card_id):
        return ["always cite the source"]


class Predictions:
    def load_predictions(self, scope_key):
        return ["next: weekly review"]

    def save_patterns(self, scope_key, patterns):
        return None

    def load_patterns(self, scope_key):
        return []

    def save_predictions(self, scope_key, predictions, views=None):
        return None

    def load_view(self, scope_key, prediction_id):
        return ""

    def clear_predictions(self, scope_key):
        return None

    def append_log(self, record):
        return None


class Reps:
    def resolve(self, task):
        return (task["assignee_rep_id"], "skills/rep")


class Directive:
    def directive(self, message):
        return "name the next milestone"


class Conversations:
    def current_slice(self, conv_id, query):
        return f"{conv_id}: {query}"

    def related_slices(self, query, scope, **options):
        return ""


def test_the_area_reads_each_store_for_one_request_and_hands_the_other_lines_to_their_members():
    area = quest_ai_runner.run(IntentHolding, environ={}, provided={
        "recall-recent-context": Recent(), "guide-by-feedback": Guidance(), "store-feedback": Ledger(),
        "persist-predictions": Predictions(), "select-a-rep": Reps(), "guide-toward-the-long-term-goal": Directive(),
        "remember-conversations": Conversations()})
    held = area.hold("quest-1", "plan the week")
    assert held["recent"][0]["scope"] == "quest-1"
    assert held["guidance"] == ["applies to: plan the week (3)"]
    assert held["owed"] == ["owed"] and held["standing"] == ["always cite the source"]
    assert held["predictions"] == ["next: weekly review"]
    assert area.rep({"assignee_rep_id": "rep-1"}) == ("rep-1", "skills/rep")
    assert area.directive("hello") == "name the next milestone"
    assert area.conversation("c1", "what next") == "c1: what next"
