"""CURRENT: the live ``FilePredictionStore`` (one atomic JSON file per scope plus a JSONL outcome log) on a
scratch folder. Zero model calls."""
import tempfile

import common  # noqa: F401  (sets sys.path for the repo)
from persist_predictions.scenarios import KINDS
from quest_ai_runner.core.anticipation_store import FilePredictionStore


def build(meter):  # noqa: ARG001
    def persist(kind, args):
        with tempfile.TemporaryDirectory(prefix="qar_pred_") as root:
            return KINDS[kind](FilePredictionStore, root, args)
    return persist
