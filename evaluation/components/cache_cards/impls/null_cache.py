"""CURRENT: the production default, ``NullCardCache``, plugged into the live ``QuestApiCardRepository``
against the in-process card API. It stores nothing and queues nothing, so a failure stays loud and
nothing survives an outage. Zero model calls (a local HTTP server stands in for the backend)."""
from cache_cards.driver import run_scenario
from quest_ai_runner.adapters.quest_api_card_repository import NullCardCache


def build(meter):  # noqa: ARG001
    def scenario(kind, args):
        if kind != "scenario":
            raise ValueError(kind)
        return run_scenario(args, NullCardCache)
    return scenario
