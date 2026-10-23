"""CURRENT: the two pipelines side by side, as a run has them today, offline, through the declared implementation
``core/unified_search.two_pipelines``.

Cards: the live card selector it is handed, ``HybridContextAssembler`` over a real ``FileContextStore`` (keyword arm,
shipped confidence gate) and a real ``VectorContextAssembler`` seeded from the store (the local embedder over an in-memory
vector store, as in context_selection's hybrid_union arm), at most three cards, no consolidating model pass. Records: what
the ``find_specs`` tool returns, the selected spec-selection implementation ``lexical_vector_gated`` with the local vector
search over the records and one link per record, no provider. The two lists are put side by side, cards first; neither
knows "no solution yet" and neither reads tags. Zero model calls."""
import tempfile

from context_and_specs.world import CARD_PREFIX, world
from context_selection.impls.hybrid_union import _MemoryVectorStore
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.core.unified_search import two_pipelines

ARM_CARDS = 3


def card_selector(cards, embed):
    keyword = FileContextStore(tempfile.mkdtemp(prefix="qar_ctxspecs_"), auto_bootstrap=False, max_cards_in_view=ARM_CARDS)
    for cid, card in cards.items():
        keyword._own_repo.write(cid, card)
    keyword._cache_dirty = True
    vector = VectorContextAssembler(_MemoryVectorStore(embed), llm_review=False, max_in_view=ARM_CARDS,
                                    max_seed_items=0, seed_source=keyword.export_for_embedding)
    return HybridContextAssembler(keyword=keyword, vector=vector, consolidate=False)


def build(meter):  # noqa: ARG001
    w = world()
    assembler = card_selector(w.cards, w.embed)

    def select_cards(task):
        ids = []
        for cid in assembler.assemble(task).card_ids:
            cid = cid[len(CARD_PREFIX):] if cid.startswith(CARD_PREFIX) else cid
            if cid in w.cards and cid not in ids:
                ids.append(cid)
        return ids[:ARM_CARDS]

    def search(task, tags):  # noqa: ARG001  find_specs reads no tags
        got = two_pipelines(task, w.views, vector_search=w.records_search, select_cards=select_cards)
        return {"items": [i.key for i in got.items], "linked": {i.key: i.linked_from for i in got.items if i.linked_from},
                "no_solution_yet": bool(got.no_solution_yet), "calls": got.calls, "route": got.route}
    return search
