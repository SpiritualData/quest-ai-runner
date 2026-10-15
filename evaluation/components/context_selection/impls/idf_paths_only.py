"""ABLATION: the same IDF store with the docstring text removed from every card, so only path tokens
and symbol names remain (the pre-docstring state). Isolates how much the docstring orientation text
contributes. Zero model calls."""
import tempfile

from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):  # noqa: ARG001
    stores = {}

    def select(task, corpus):
        key = str(corpus.root)
        if key not in stores:
            store = FileContextStore(tempfile.mkdtemp(prefix="qar_ctxsel_"), repo_root=str(corpus.root),
                                     auto_bootstrap=False, confidence_threshold=0.0, max_cards_in_view=10)
            for cid, card in corpus.cards.items():
                store._own_repo.write(cid, {**card, "summary": "", "description": ""})
            store._cache_dirty = True
            stores[key] = store
        ctx = stores[key].assemble(task)
        cards = stores[key]._load_all()
        return [fe["path"] for cid in ctx.card_ids for fe in cards.get(cid, {}).get("files", [])]
    return select
