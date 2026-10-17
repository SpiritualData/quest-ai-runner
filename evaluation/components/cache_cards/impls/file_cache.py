"""CANDIDATE: the durable ``FileCardCache`` (two atomic JSON files in a directory) plugged into the same
live ``QuestApiCardRepository``. It is the only implementation here that survives a process restart.
Zero model calls."""
import tempfile

from cache_cards.driver import run_scenario
from quest_ai_runner.adapters.card_cache_file import FileCardCache


def build(meter):  # noqa: ARG001
    def scenario(kind, args):
        if kind != "scenario":
            raise ValueError(kind)
        with tempfile.TemporaryDirectory(prefix="qar_card_cache_") as directory:
            return run_scenario(args, lambda: FileCardCache(directory))
    return scenario
