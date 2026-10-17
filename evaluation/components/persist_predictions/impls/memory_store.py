"""ALTERNATIVE: an in-process store that satisfies the ``PredictionStore`` port with plain dicts. It is the
reference a consumer with its own database would match; it keeps nothing across store objects, so it fails the
restart scenario by design. Zero model calls."""
import time

import common  # noqa: F401  (sets sys.path for the repo)
from persist_predictions.scenarios import KINDS

ALTERNATIVE = True


class MemoryStore:
    def __init__(self, root=None):
        self.patterns, self.predictions, self.views, self.log_records = {}, {}, {}, []

    def load_patterns(self, key):
        return list(self.patterns.get(key, []))

    def save_patterns(self, key, patterns):
        self.patterns[key] = list(patterns)

    def load_predictions(self, key):
        return [p for p in self.predictions.get(key, []) if p.expires_ts > time.time()]

    def save_predictions(self, key, predictions, views=None):
        self.predictions[key] = list(predictions)
        self.views[key] = dict(views or {})

    def load_view(self, key, prediction_id):
        return self.views.get(key, {}).get(prediction_id, "")

    def clear_predictions(self, key):
        self.predictions[key] = []
        self.views[key] = {}

    def append_log(self, record):
        self.log_records.append(record)


def build(meter):  # noqa: ARG001
    def persist(kind, args):
        return KINDS[kind](MemoryStore, "", args)
    return persist
