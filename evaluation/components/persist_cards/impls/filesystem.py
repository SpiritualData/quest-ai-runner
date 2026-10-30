"""The live ``FilesystemCardRepository`` (one atomic JSON file per card) behind the ``CardRepository`` port, run
through the shared conformance scenarios (see ``backends.py``). Zero model calls."""
from persist_cards.backends import FolderHarness, on_backend
from quest_ai_runner.config_assembler import filesystem_card_repository


def build(meter):  # noqa: ARG001
    return on_backend(FolderHarness, lambda harness: filesystem_card_repository(harness.root))
