"""FLOOR: a store that accepts every write and keeps nothing, the failure the port exists to prevent
(an engine that relearns every turn). Zero model calls."""
import common  # noqa: F401  (sets sys.path for the repo)
from persist_predictions.scenarios import KINDS


class ForgetfulStore:
    def __init__(self, root=None):
        pass

    def load_patterns(self, key):
        return []

    def save_patterns(self, key, patterns):
        pass

    def load_predictions(self, key):
        return []

    def save_predictions(self, key, predictions, views=None):
        pass

    def load_view(self, key, prediction_id):
        return ""

    def clear_predictions(self, key):
        pass

    def append_log(self, record):
        pass


def build(meter):  # noqa: ARG001
    def persist(kind, args):
        return KINDS[kind](ForgetfulStore, "", args)
    return persist
