"""FLOOR: a store that keeps nothing (loads are empty, writes vanish). What any real store must beat."""
import persist_predictions.driver as driver


class NoStore:
    log_path = "/nonexistent/prediction_log.jsonl"

    def save_patterns(self, scope_key, patterns): ...
    def load_patterns(self, scope_key): return []
    def save_predictions(self, scope_key, predictions, views=None): ...
    def load_predictions(self, scope_key): return []
    def load_view(self, scope_key, prediction_id): return ""
    def clear_predictions(self, scope_key): ...
    def append_log(self, record): ...


def build(meter):  # noqa: ARG001
    def persist(kind, args):
        return driver.run_ops(args, NoStore())
    return persist
