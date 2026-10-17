"""The live ``FileRecentContextStore`` (record, scoped load with the cross-quest fence) followed by the live
``filter_relevant`` gate, over a fresh temp directory per case. Nothing is faked: it is a local store and the
gate is pure lexical code."""
import tempfile

from quest_ai_runner.core.recent_context import FileRecentContextStore, filter_relevant


def build(meter):  # noqa: ARG001
    def recall(case):
        store = FileRecentContextStore(root_dir=tempfile.mkdtemp(prefix="qar_recent_"))
        for turn in case["turns"]:
            store.record(turn["scope_keys"], turn["cards"], turn["user_text"])
        records = store.load(case["load_keys"])
        kept = filter_relevant(records, case["query"], is_followup=case["followup"])
        return {"ids": [r["id"] for r in kept]}
    return recall
