"""The whole runner as one function: ``quest_ai_runner.run("quest-ai-runner", message)`` and the seats it is built from."""
import pytest
from esoi.entry import Pending
from hubofhubs.card import Card
from hubofhubs.cardstore import LocalCardStore  # noqa: F401 (declares the in-memory card-store arm)

import quest_ai_runner
from quest_ai_runner.loop_wiring import GoalAnswer
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


def test_the_library_takes_its_model_and_recall_by_the_interfaces_of_their_own_solutions():
    """The edges do-grounded-work -> call-a-model and -> remember-and-recall are typed needs on the composition: the model seat is
    call-a-model's own interface (a subtype of the hub's, so a plain hub provider still fills it) and the recall seat is
    remember-and-recall's own."""
    import typing
    from hubofhubs.seats import ModelCalling, Remembering
    from quest_ai_runner.core.planner_context import CardRecall
    from quest_ai_runner.goal_agent import QuestAiRunnerLibrary
    from quest_ai_runner.providers import RoutedModelCalling
    hints = typing.get_type_hints(QuestAiRunnerLibrary.__init__)
    assert hints["model"] is RoutedModelCalling and issubclass(RoutedModelCalling, ModelCalling)
    assert hints["memory"] is CardRecall and issubclass(CardRecall, Remembering)


def test_the_step_reasoning_arm_takes_its_model_by_call_a_models_own_interface():
    import typing
    from quest_ai_runner.area_reason_and_answer import RebuiltPerStep
    from quest_ai_runner.providers import RoutedModelCalling
    assert typing.get_type_hints(RebuiltPerStep.__init__)["model"] is RoutedModelCalling
