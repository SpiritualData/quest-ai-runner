"""Qualitative end-to-end eval of the CARD system with a REAL model.

Round-trip: (1) several deep tasks -> the real async updater LEARNS user cards (each referencing a
collection); (2) for new requests, the card store SELECTS cards and RESOLVES their references
fresh; we score selection precision + resolution + no-irrelevant-leak. Stub collection resolver
returns a distinctive string per collection so we can see exactly what got pulled in.
"""
import os, sys, json, tempfile
from pathlib import Path

# Repo root derived from THIS file (evaluation/<this>.py) so the harness is path-agnostic — no
# absolute machine path hardcoded (public-repo hard rule #1). Reads the repo's local .env for the
# real model + Voyage credentials this qualitative eval needs.
REPO = str(Path(__file__).resolve().parents[1])
for line in (Path(REPO) / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
sys.path.insert(0, REPO)
from quest_ai_runner.cli import _config_from_env
from quest_ai_runner.config import build_orchestrator
from quest_ai_runner.adapters.file_context_store import FileContextStore


class StubCollectionResolver:
    """Resolves a collection reference to FRESH, identifiable rows so we can verify (a) the card was
    selected and (b) its reference resolved live for the right query."""
    def __init__(self):
        self.calls = []
    def resolve(self, locator, *, max_chars=2000):
        self.calls.append(locator)
        name = locator.get("name", "?"); cid = locator.get("id", "?")
        return f"[FRESH ROWS from collection '{name}' ({cid})]: 3 recent entries for {name}."


# Topics to LEARN (deep-task simulations). future_context names a collection with an id.
LEARN = [
    ("Analyze my recent dreams and any stress link.",
     "Summarized the user's dream journal: water/falling motifs in high-stress weeks.",
     "- Dream journal is the collection 'Dream Journal' (id col_dreams_8821).\n"
     "- Daily stress is tracked in collection 'Daily Mood' (id col_mood_4410)."),
    ("Review the open pull requests on my backend repo.",
     "Listed open PRs and flagged two with failing tests.",
     "- The user's code review notes live in the collection 'Code Reviews' (id col_rev_5512)."),
    ("Plan my upcoming trip itinerary.",
     "Drafted a 5-day itinerary and flagged booking gaps.",
     "- The user's trip plans are in the collection 'Trip Plans' (id col_trip_3300)."),
]

# New requests to evaluate retrieval+resolution. (query, expected collection id resolved, label)
EVAL = [
    ("what keeps showing up in my dreams lately?", "col_dreams_8821", "dreams"),
    ("how has my mood been trending day to day?", "col_mood_4410", "mood"),
    ("any pull requests i should look at?", "col_rev_5512", "code"),
    ("remind me about my travel itinerary", "col_trip_3300", "trip"),
    ("what's a good recipe for banana bread?", None, "unrelated (expect NO card)"),
]


def main():
    cards_dir = Path(tempfile.mkdtemp(prefix="qar_card_quality_"))
    qdir = Path(tempfile.mkdtemp(prefix="qar_card_qdrant_"))
    resolver = StubCollectionResolver()
    cfg = _config_from_env()
    orch = build_orchestrator(cfg)

    # REAL backend recipe locally: keyword FileContextStore over a Qdrant card repo, fused with a
    # query-only Voyage vector arm over the SAME embedded collection (one client, embedded path).
    from qdrant_client import QdrantClient
    from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder
    from quest_ai_runner.adapters import (
        QdrantCardRepository, QdrantCardVectorStore, VectorContextAssembler, HybridContextAssembler,
    )
    client = QdrantClient(path=str(qdir))
    scope = {"user_id": "evaluser"}
    repo = QdrantCardRepository(client=client, collection="cards",
                                embedder=make_voyage_embedder(input_type="document"),
                                vector_size=1024, scope=scope)
    keyword = FileContextStore(str(cards_dir), auto_bootstrap=False, confidence_threshold=0.0,
                               provider=orch.provider, reference_resolvers={"collection": resolver},
                               card_repository=repo)
    vstore = QdrantCardVectorStore(client=client, collection="cards",
                                   query_embedder=make_voyage_embedder(input_type="query"),
                                   scope=scope)
    # Mirror the backend recipe (quest_ai_card_store._build_qdrant_backend): the vector arm gets the
    # SAME reference resolvers as the keyword arm (so a vector-selected card resolves its references
    # fresh) and a card-tuned confidence floor (so an unrelated query returns no card). 0.45 is the
    # backend default (_DEFAULT_VECTOR_MIN_SCORE), calibrated against this eval's cosine separation.
    vec = VectorContextAssembler(vstore, provider=orch.provider, seed_source=None,
                                 reference_resolvers={"collection": resolver},
                                 confidence_min_score=0.45)
    store = HybridContextAssembler(keyword=keyword, vector=vec)
    orch.context_assembler = store
    print(f"# model={orch.registry.resolve_tier(orch.cfg.planner_tier)}  (real Voyage vector arm)\n")

    # --- Phase 1: LEARN cards via the real async updater ---
    print("=== PHASE 1: learn cards (real updater) ===")
    for req, executed, fut in LEARN:
        n = orch._update_cards_after_deep(request=req, executed=executed, future_context=fut,
                                          ctx_meta={"user_id": "evaluser"})
        print(f"  learned {n} card(s) for: {req[:50]}")
    cards = list(repo.load_all().values())
    coll_ids = {it.get("locator", {}).get("id") for c in cards for it in c.get("content", [])
                if it.get("type") == "collection"}
    print(f"  -> {len(cards)} cards on disk; collection refs learned: {sorted(x for x in coll_ids if x)}\n")

    # --- Phase 2: qualitative retrieval + resolution eval ---
    print("=== PHASE 2: select + resolve for new requests ===")
    sel_hits = 0; leak = 0; n_expected = 0
    for query, expect_id, label in EVAL:
        asm = store.assemble(query)
        view = asm.context_view or ""
        titles = [c.get("title") or c.get("id") for c in (asm.card_metadata or [])]
        # which learned collections resolved into the view?
        resolved = sorted({cid for cid in coll_ids if cid and cid in view})
        if expect_id is None:
            ok = len(resolved) == 0
            leak += (0 if ok else 1)
            print(f"  [{ 'OK' if ok else 'LEAK'}] {label!r}  selected={titles}  resolved={resolved}")
        else:
            n_expected += 1
            ok = expect_id in view
            sel_hits += ok
            other = [c for c in resolved if c != expect_id]
            print(f"  [{'OK' if ok else 'MISS'}] {label}: q={query!r}")
            print(f"        selected_cards={titles}  resolved={resolved}"
                  f"{'  (+irrelevant: ' + str(other) + ')' if other else ''}")
    print()
    print("=== SUMMARY ===")
    print(f"  correct card selected + reference resolved: {sel_hits}/{n_expected}")
    print(f"  unrelated query stayed clean (no card):     {1 - leak}/1")
    print(f"  resolver was called {len(resolver.calls)} time(s) total")


if __name__ == "__main__":
    main()
