"""LIVE ARM: the original ``card_quality_eval.py`` flow behind the component interface. (1) The real async card
updater (``Orchestrator._update_cards_after_deep``, a model call per learned task) writes cards into a Qdrant
card repository; (2) the store is the production backend recipe: a keyword ``FileContextStore`` over that
repository fused with a query-only Voyage vector arm (``HybridContextAssembler``, vector floor 0.45), both
resolving ``collection`` references through the stub resolver; (3) each query is assembled and the view returned.

Needs a model backend (``QAR_MODEL_BACKEND``), ``qdrant_client`` and a Voyage key (``VOYAGE_API_KEY``). It was moved
here from the script without a live run (no model on the host that did the move), so treat its first run as a
shake-down. It cannot run, and so cannot be recorded, offline (waivers.csv S10 persist-cards live arm)."""
import os
import tempfile
from pathlib import Path

import common
from card_quality.impls.seeded_keyword_store import StubCollectionResolver


def build(meter):
    if not os.environ.get("VOYAGE_API_KEY"):
        raise SystemExit("live_updater_hybrid needs VOYAGE_API_KEY (and a model backend); use --impl seeded_keyword_store offline")
    from qdrant_client import QdrantClient
    from quest_ai_runner.adapters import (
        HybridContextAssembler, QdrantCardRepository, QdrantCardVectorStore, VectorContextAssembler)
    from quest_ai_runner.adapters.file_context_store import FileContextStore
    from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder

    state = {}

    def seed(world):
        orch = common.build_offline_orchestrator(meter)
        resolver = StubCollectionResolver()
        client = QdrantClient(path=tempfile.mkdtemp(prefix="qar_card_qdrant_"))
        scope = {"user_id": "evaluser"}
        repo = QdrantCardRepository(client=client, collection="cards", embedder=make_voyage_embedder(input_type="document"),
                                    vector_size=1024, scope=scope)
        keyword = FileContextStore(tempfile.mkdtemp(prefix="qar_card_quality_"), auto_bootstrap=False,
                                   confidence_threshold=0.0, provider=orch.provider,
                                   reference_resolvers={"collection": resolver}, card_repository=repo)
        vstore = QdrantCardVectorStore(client=client, collection="cards",
                                       query_embedder=make_voyage_embedder(input_type="query"), scope=scope)
        vec = VectorContextAssembler(vstore, provider=orch.provider, seed_source=None,
                                     reference_resolvers={"collection": resolver}, confidence_min_score=0.45)
        store = HybridContextAssembler(keyword=keyword, vector=vec)
        orch.context_assembler = store
        for item in world["learn"]:
            orch._update_cards_after_deep(request=item["request"], executed=item["executed"],
                                          future_context=item["future_context"], ctx_meta={"user_id": "evaluser"})
        return store

    def select(query, world):
        if "store" not in state:
            state["store"] = seed(world)
        assembled = state["store"].assemble(query)
        return {"view": assembled.context_view or "",
                "titles": [c.get("title") or c.get("id") for c in (assembled.card_metadata or [])]}
    return select
