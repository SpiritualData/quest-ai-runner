"""The the-subconscious area: its ids answer offline by the strict definition, and the arm recalls over the card store it is handed."""
import pytest
from esoi import Pending, targets
from hubofhubs.api import Card
from hubofhubs.api import LocalCardStore  # noqa: F401 (declares the in-memory card-store arm)

import quest_ai_runner
from quest_ai_runner.the_intelligence.subconscious.interface import CardRecall

AREA_IDS = ("the-subconscious", "card-store")


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


def test_the_area_pends_on_the_quest_keys_only():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("the-subconscious", "rust release", environ={})
    text = " ".join(caught.value.lines)
    assert "QUEST_API_KEY" in text and "QUEST_BASE_URL" in text


def test_the_arm_recalls_the_cards_of_the_store_it_is_handed():
    cards = quest_ai_runner.run("card-store", arms={"card-store": "local_card_store"}, environ={})
    cards.add(Card(id="goal-1", text="Finish the book outline by Friday", tags=("book",)))
    memory = quest_ai_runner.run(CardRecall, environ={}, provided={"card-store": cards})
    assert [card.id for card in memory.recall("book outline")] == ["goal-1"]
    assert memory.recall("nothing like this") == []
    prepared = memory.prepare("book outline")
    assert [card.id for card in prepared["cards"]] == ["goal-1"] and {step["solution"] for step in prepared["steps"]} == {"the-intuition", "context-retriever"}
    assembled = memory.assemble("book outline")
    assert assembled.card_ids == ["goal-1"] and "Finish the book outline" in assembled.context_view
