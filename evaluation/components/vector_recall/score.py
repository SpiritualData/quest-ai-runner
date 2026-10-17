"""Score one vector-recall case: the labelled document is in the top 3 hits. ``rank`` is its 1-based position
(None when absent). The handbook is pinned by ``corpus_sha`` in each case (it is the retrieval component's file)."""
from retrieval.score import corpus_sha
from retrieval.world import load_corpus
from vector_recall.interface import TOP_K


def score_case(fn, case):
    if case.get("corpus_sha") != corpus_sha():
        raise RuntimeError("retrieval/corpus.json changed since these cases were labelled; relabel and update corpus_sha")
    ids = fn(case["query"], load_corpus()).get("ids", [])
    want = case["relevant"][0]
    rank = ids.index(want) + 1 if want in ids else None
    return dict(ok=rank is not None and rank <= TOP_K, top1=rank == 1, rank=rank,
                got=f"rank {rank}" if rank else "miss", note=f"{want} <- {ids[0] if ids else 'nothing'}")


def aggregate(rows):
    n = len(rows) or 1
    by_area = {}
    for r in rows:
        by_area.setdefault(r.get("area"), []).append(r["ok"])
    parts = ", ".join(f"{a} {sum(v)}/{len(v)}" for a, v in sorted(by_area.items(), key=lambda kv: str(kv[0])))
    return (f"top-3 {sum(r['ok'] for r in rows)}/{len(rows)} ({sum(r['ok'] for r in rows) / n:.0%}), "
            f"top-1 {sum(r['top1'] for r in rows)}/{len(rows)} ({parts})")
