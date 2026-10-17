"""The live ``QuestApiCardRepository`` (the selected backend) against the in-process fake of the Quest card API,
behind the ``CardRepository`` port, run through the shared conformance scenarios (see ``backends.py``). Zero model
calls."""
from persist_cards.backends import QuestApiHarness, on_backend
from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository


def build(meter):  # noqa: ARG001
    return on_backend(
        QuestApiHarness,
        lambda harness: QuestApiCardRepository(base_url=harness.backend.url, api_key="fake-key", timeout=5.0))
