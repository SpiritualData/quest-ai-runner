"""HUB CANDIDATE: the hub's selected free ``cards-from-files`` arm (``per_file_symbols``: one card per file from its docstrings and symbols),
built through QAR's Assembler like production resolves the seat, then ranked by the same fixed IDF store as ``idf_docstrings``. Zero model
calls. It is a file-finding baseline: its store holds a card per file, so the topic cases refuse it and it is never a candidate for the
production card store."""
import tempfile

from context_selection.store_cost import store_charged
from quest_ai_runner import hub
from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):
    stores = {}

    def select(task, corpus):
        key = corpus.key
        if key not in stores:
            assert not corpus.extra_roots, "the hub arm builds cards from one root"
            seat = hub.child().resolve("cards-from-files", name="per_file_symbols")()
            store = FileContextStore(tempfile.mkdtemp(prefix="qar_ctxsel_"), repo_root=str(corpus.root), auto_bootstrap=False,
                                     confidence_threshold=0.0, max_cards_in_view=10)
            for card in seat.build(str(corpus.root), corpus.files, {}):
                store._own_repo.write(card["id"], card)
            store._cache_dirty = True
            stores[key] = store
        ctx = stores[key].assemble(task)
        cards = stores[key]._load_all()
        return [fe["path"] for cid in ctx.card_ids for fe in cards.get(cid, {}).get("files", [])]
    return store_charged(select, meter)
