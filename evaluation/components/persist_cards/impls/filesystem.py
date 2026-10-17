"""The live ``FilesystemCardRepository`` (one atomic JSON file per card) behind the ``CardRepository`` port, run
through the shared conformance scenarios (see ``backends.py``). Zero model calls."""
from persist_cards.backends import FolderHarness, on_backend
from quest_ai_runner.adapters.card_repository import FilesystemCardRepository


def build(meter):  # noqa: ARG001
    return on_backend(FolderHarness, lambda harness: FilesystemCardRepository(harness.root))
