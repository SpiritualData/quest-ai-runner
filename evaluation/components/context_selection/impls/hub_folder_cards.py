"""ALTERNATIVE (the hub's own cards): one card per folder, grouped and described with the hub's own card helpers (``file_card``, ``group_card``) that the hub's
cards-from-files arms are made of, then ranked by the same fixed IDF store. Zero model calls. A card per folder is a store far under the
topic cap, so this is the free arm the topic cases can pass; its file-finding is poor because a card lists all of a folder's files."""
import posixpath
import tempfile

from context_selection.store_cost import store_charged
from hubofhubs.recall.cards_from_files import file_card, group_card
from quest_ai_runner.adapters.file_context_store import FileContextStore


def folder_cards(root, files):
    groups = {}
    for rel in files:
        groups.setdefault(posixpath.dirname(rel), []).append(rel)
    singles = {rel: file_card(root, rel) for rel in files}
    return [group_card(root, f"folder-{index:04d}", members, singles) for index, members in enumerate(groups.values())]


def build(meter):
    stores = {}
    sizes = {}

    def select(task, corpus):
        key = corpus.key
        if key not in stores:
            assert not corpus.extra_roots, "the baseline builds cards from one root"
            store = FileContextStore(tempfile.mkdtemp(prefix="qar_ctxsel_"), repo_root=str(corpus.root), auto_bootstrap=False,
                                     confidence_threshold=0.0, max_cards_in_view=10)
            cards = folder_cards(corpus.root, corpus.files)
            for card in cards:
                store._own_repo.write(card["id"], card)
            sizes[key] = len(cards)
            store._cache_dirty = True
            stores[key] = store
        ctx = stores[key].assemble(task)
        cards = stores[key]._load_all()
        return [fe["path"] for cid in ctx.card_ids for fe in cards.get(cid, {}).get("files", [])]
    return store_charged(select, meter, cards=lambda corpus: sizes.get(corpus.key, 0))
