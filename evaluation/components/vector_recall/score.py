"""Score one vector-recall case. A recall case: the labelled document is in the top 3 hits (``rank`` is its 1-based
position, None when absent); the handbook is pinned by ``corpus_sha`` in each case (it is the retrieval
component's file). A latency case (``kind`` latency): the same labelled queries are run against a corpus padded
to a FIXED size with deterministic filler cards (``filler.py``); the first search indexes the corpus and is timed
apart as ``index_ms``, then every query is timed ``repeats`` times. The case passes when p50 and p95 query
milliseconds are within the caps written in the case (``cap_basis`` says where they come from); the labelled
recall at that size is reported beside them."""
import json
import time
from pathlib import Path

from retrieval.score import corpus_sha
from retrieval.world import load_corpus
from vector_recall.filler import padded
from vector_recall.interface import TOP_K

HERE = Path(__file__).resolve().parent
LATENCY = {}  # the latest latency measurement of this run, for the headline


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))]


def score_latency(fn, case):
    labelled = [c for c in json.loads((HERE / "cases.json").read_text()) if c.get("query")]
    corpus = padded(load_corpus(), case["corpus_size"])
    started = time.perf_counter()
    fn(labelled[0]["query"], corpus)
    index_ms = (time.perf_counter() - started) * 1000
    for c in labelled[: case.get("warmup", 2)]:
        fn(c["query"], corpus)
    samples, hits = [], 0
    for repeat in range(case["repeats"]):
        for c in labelled:
            t0 = time.perf_counter()
            ids = fn(c["query"], corpus).get("ids", [])
            samples.append((time.perf_counter() - t0) * 1000)
            if repeat == 0 and c["relevant"][0] in ids[:TOP_K]:
                hits += 1
    p50, p95 = percentile(samples, 0.5), percentile(samples, 0.95)
    LATENCY.update(p50=p50, p95=p95, index=index_ms, size=case["corpus_size"], hits=hits, of=len(labelled),
                   p50_cap=case["p50_cap_ms"], p95_cap=case["p95_cap_ms"])
    ok = p50 <= case["p50_cap_ms"] and p95 <= case["p95_cap_ms"]
    return dict(ok=ok, top1=False, rank=None, p50_ms=round(p50, 1), p95_ms=round(p95, 1), index_ms=round(index_ms),
                recall_at_size=f"{hits}/{len(labelled)}", got=f"p95 {p95:.0f} ms",
                note=f"{case['corpus_size']} cards: p50 {p50:.1f} ms (cap {case['p50_cap_ms']}), p95 {p95:.1f} ms "
                     f"(cap {case['p95_cap_ms']}), {len(samples)} queries, index {index_ms:.0f} ms, "
                     f"top-3 at this size {hits}/{len(labelled)}")


def score_case(fn, case):
    if case.get("corpus_sha") != corpus_sha():
        raise RuntimeError("retrieval/corpus.json changed since these cases were labelled; relabel and update corpus_sha")
    if case.get("kind") == "latency":
        return score_latency(fn, case)
    ids = fn(case["query"], load_corpus()).get("ids", [])
    want = case["relevant"][0]
    rank = ids.index(want) + 1 if want in ids else None
    return dict(ok=rank is not None and rank <= TOP_K, top1=rank == 1, rank=rank,
                got=f"rank {rank}" if rank else "miss", note=f"{want} <- {ids[0] if ids else 'nothing'}")


def aggregate(rows):
    recall = [r for r in rows if r.get("area") != "latency"]
    n = len(recall) or 1
    by_area = {}
    for r in recall:
        by_area.setdefault(r.get("area"), []).append(r["ok"])
    parts = ", ".join(f"{a} {sum(v)}/{len(v)}" for a, v in sorted(by_area.items(), key=lambda kv: str(kv[0])))
    text = (f"top-3 {sum(r['ok'] for r in recall)}/{len(recall)} ({sum(r['ok'] for r in recall) / n:.0%}), "
            f"top-1 {sum(r['top1'] for r in recall)}/{len(recall)} ({parts})")
    for r in rows:
        if r.get("area") == "latency":
            text += (f"; latency at {LATENCY.get('size')} cards: p50 {r['p50_ms']} ms, p95 {r['p95_ms']} ms "
                     f"(caps {LATENCY.get('p50_cap')}/{LATENCY.get('p95_cap')} ms), index {r['index_ms']} ms, "
                     f"top-3 at that size {r['recall_at_size']}")
    return text
