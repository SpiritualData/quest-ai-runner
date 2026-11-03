"""The whole runner as one function: ``quest_ai_runner.run("quest-ai-runner", message)`` and the seats it is built from."""
import pytest
from esoi.entry import Pending
from hubofhubs.card import Card
from hubofhubs.cardstore import LocalCardStore  # noqa: F401 (declares the in-memory card-store arm)

import quest_ai_runner
from quest_ai_runner.goal_agent import GoalAnswer
from tests.conftest import StubProvider


class ScriptedModel(StubProvider):
    """A scripted model that cannot search the web."""

    supports_web_search = False

    def web_search(self, *args, **kwargs):
        raise NotImplementedError


class QuestStub:
    """The Quest account the runner works in, as a caller passes it."""

    base_url, api_key, team_id = "https://quest.example.org", "key", ""

    def list_quest_goals(self, quest_id, **options):
        return []


def test_without_keys_the_whole_runner_pends_on_keys_only():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("quest-ai-runner", "What should I work on first this week?", environ={})
    text = " ".join(caught.value.lines)
    assert "QUEST_API_KEY" in text and "QUEST_BASE_URL" in text
    assert "pass options" not in text.split("card-store (qar-card-repository)")[0]


def test_each_seat_has_a_default_that_pends_on_a_key_not_on_an_option():
    for seat in ("card-store", "access-quest-operations"):
        with pytest.raises(Pending) as caught:
            quest_ai_runner.run(seat, "x", environ={})
        assert "QUEST_API_KEY" in " ".join(caught.value.lines)


def test_keys_build_every_seat_from_the_environment():
    environ = {"QUEST_BASE_URL": "https://quest.example.org", "QUEST_API_KEY": "key"}
    cards = quest_ai_runner.run("card-store", environ=environ)
    assert cards.repository.base_url == "https://quest.example.org"


def test_the_runner_answers_over_the_seats_it_is_given():
    provider = ScriptedModel([{"action": "answer", "rationale": "known", "model_tier": "sonnet"}], answer_text="Start with the outline.")
    cards = quest_ai_runner.run("card-store", arms={"card-store": "local_card_store"}, environ={})
    cards.add(Card(id="goal-1", text="Finish the book outline by Friday", tags=("book",)))
    answer = quest_ai_runner.run("quest-ai-runner", "What should I work on first this week?", environ={},
                                 provided={"call-a-model": provider, "card-store": cards, "access-quest-operations": QuestStub()})
    assert isinstance(answer, GoalAnswer) and answer.kind == "answer"
    assert "Start with the outline." in answer.text
    assert provider.answer_calls >= 1
