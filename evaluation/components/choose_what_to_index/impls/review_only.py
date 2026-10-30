"""ALTERNATIVE: the model-judged folder review as it was before the risk gate.

The library's ``review_corpus`` with no risk signals, which is what ``folder_review`` still returns: below the
2,000-file floor no folder is judged, and when the review is switched off, has no provider or fails, nothing is
excluded (fail-open). The deterministic safeguards (nested repositories, fuzzy duplicates) run above the floor."""
from pathlib import Path

from hubofhubs.recall.context_folder_review import folder_review
from quest_ai_runner.adapters.card_store_settings import STORE_DIR_NAME, folder_review_enabled, skip_nested_repos
from quest_ai_runner.adapters.context_risk_gate import FolderGate


def build(meter):  # noqa: ARG001
    def gate(walk_root, skip_dirs, provider, model, cards_dir, *, persist=True):  # noqa: ARG001
        excluded = set()
        if folder_review_enabled():
            try:
                excluded = folder_review(Path(walk_root), skip_dirs, provider, model, Path(cards_dir),
                                         store_dir_name=STORE_DIR_NAME, skip_nested_repos=skip_nested_repos())
            except Exception:  # noqa: BLE001 -- the review that predates the gate failed open
                excluded = set()
        return FolderGate(walk_root=Path(walk_root).resolve(), excluded=set(excluded))
    return gate
