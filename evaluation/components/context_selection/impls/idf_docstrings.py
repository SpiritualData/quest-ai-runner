"""CURRENT: the production keyword arm. ``FileContextStore`` field-weighted IDF over each file's card
(filename, symbols, docstring-derived summary and description), with the confidence gate off so it
always returns a ranking. Zero model calls."""
import tempfile

from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):  # noqa: ARG001
    stores = {}

    def select(task, corpus):
        key = str(corpus.root)
        if key not in stores:
            cards_dir = tempfile.mkdtemp(prefix="qar_ctxsel_")
            store = FileContextStore(cards_dir, repo_root=str(corpus.root), auto_bootstrap=False,
                                     confidence_threshold=0.0, max_cards_in_view=10)
            for cid, card in corpus.cards.items():
                store._own_repo.write(cid, card)
            store._cache_dirty = True
            stores[key] = store
        ctx = stores[key].assemble(task)
        cards = stores[key]._load_all()
        return [fe["path"] for cid in ctx.card_ids for fe in cards.get(cid, {}).get("files", [])]
    return select
