"""CURRENT: the live ``HybridContextAssembler`` (keyword + vector arms fused), driven offline.

Nothing is re-implemented here. The keyword arm is the real ``FileContextStore`` over the corpus cards (as in
``idf_docstrings``). The vector arm is the real ``VectorContextAssembler``, seeded through its own
``seed_source`` (``FileContextStore.export_for_embedding``), over a small in-memory ``VectorStore`` whose only
local part is the embedder (bge-small, offline, cached; see ``embedder.py``) and cosine. The fusion, union
order, labelled sections and deadline logic are the live class's. No provider is wired, so the consolidating
model pass is off and the harness stays at zero model calls.

Each arm is capped at 3 cards, so the union is at most 6 files, declared with ``select.budget = 6``: the
scorer reports the strict top-3 (live order: keyword cards first, then vector cards) and recall within 6.
"""
import tempfile

from context_selection.embedder import embedder
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.core.adapters_context import VectorHit

ARM_CARDS = 3


class _MemoryVectorStore:
    """The smallest VectorStore: holds embedded items, answers cosine search. Never raises."""

    def __init__(self, embed):
        self._embed, self._items, self._mat = embed, [], None

    def sync(self, items, *, scope=None):  # noqa: ARG002
        import numpy as np
        self._items = list(items)
        mat = self._embed([i.get("text") or i["id"] for i in self._items])
        self._mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        return len(self._items)

    def upsert(self, items, *, scope=None):  # noqa: ARG002
        return None

    def search(self, query, *, scope=None, top_k=8):  # noqa: ARG002
        import numpy as np
        if self._mat is None:
            return []
        q = self._embed([query])[0]
        sims = self._mat @ (q / (np.linalg.norm(q) + 1e-9))
        return [VectorHit(id=self._items[i]["id"], score=float(sims[i]), text=self._items[i].get("text", ""),
                          payload=dict(self._items[i].get("payload") or {}))
                for i in np.argsort(-sims, kind="stable")[:top_k]]

    def count(self, *, scope=None):  # noqa: ARG002
        return len(self._items)


def build(meter):  # noqa: ARG001
    embed = embedder()
    live = {}

    def assembler_for(corpus):
        key = str(corpus.root)
        if key not in live:
            keyword = FileContextStore(tempfile.mkdtemp(prefix="qar_ctxsel_"), repo_root=str(corpus.root),
                                       auto_bootstrap=False, confidence_threshold=0.0, max_cards_in_view=ARM_CARDS)
            for cid, card in corpus.cards.items():
                keyword._own_repo.write(cid, card)
            keyword._cache_dirty = True
            vector = VectorContextAssembler(_MemoryVectorStore(embed), llm_review=False, max_in_view=ARM_CARDS,
                                            max_seed_items=0, seed_source=keyword.export_for_embedding)
            live[key] = HybridContextAssembler(keyword=keyword, vector=vector, consolidate=False)
        return live[key]

    def select(task, corpus):
        ctx = assembler_for(corpus).assemble(task)
        paths = []
        for cid in ctx.card_ids:
            card = corpus.cards.get(cid[len("card:"):] if cid.startswith("card:") else cid)
            for f in (card or {}).get("files", []):
                if f["path"] not in paths:
                    paths.append(f["path"])
        return paths
    select.budget = 2 * ARM_CARDS
    return select
