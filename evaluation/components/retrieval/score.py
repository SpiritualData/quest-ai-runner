"""Score one retrieval case: recall@3 of the labelled relevant documents, plus whether the labelled answer
phrase is in the text the adapter's own read returned for each relevant document that was found.

``ok`` = every relevant document in the top 3 AND each has its answer phrase in the text the adapter read (found
but unreadable, e.g. cut off by a render cap, is not evidence gathered). ``recall`` = fraction of them in the top
3. ``top1`` = the first returned id is relevant. ``read_ok`` = every relevant document found is readable.
``corpus_sha`` in each case pins the corpus the labels were written against (``corpus.json`` is not hashed by
``results.csv``, ``cases.json`` is), so a corpus edit without relabelling fails loudly instead of drifting."""
import hashlib

from retrieval.interface import TOP_K
from retrieval.world import HERE, load_corpus


def corpus_sha():
    return hashlib.sha256((HERE / "corpus.json").read_bytes()).hexdigest()[:8]


def score_case(fn, case):
    if case.get("corpus_sha") != corpus_sha():
        raise RuntimeError(f"corpus.json changed (sha {corpus_sha()}) since these cases were labelled "
                           f"({case.get('corpus_sha')}); relabel the cases and update corpus_sha")
    out = fn(case["question"], load_corpus())
    ids, texts = out.get("ids", []), out.get("texts", {})
    top = ids[:TOP_K]
    relevant = case["relevant"]
    found = [r for r in relevant if r in top]
    recall = len(found) / len(relevant)
    read_ok = bool(found) and all(case["answers"][r].lower() in (texts.get(r) or "").lower() for r in found)
    return dict(ok=recall == 1.0 and read_ok, recall=recall, top1=bool(top) and top[0] in relevant, read_ok=read_ok,
                got=",".join(top) or "(none)",
                note="ok" if recall == 1.0 and read_ok else (f"missing {sorted(set(relevant) - set(found))}" if recall < 1.0
                                                     else "found but the answer is not in the text read"))


def aggregate(rows):
    n = len(rows) or 1
    ok, top1, read = (sum(bool(r.get(k)) for r in rows) for k in ("ok", "top1", "read_ok"))
    mean = sum(r.get("recall", 0.0) for r in rows) / n
    return (f"gathered (recall@{TOP_K} complete and readable) {ok}/{len(rows)} ({ok / n:.0%}), mean recall {mean:.0%}, "
            f"top-1 {top1}/{len(rows)}, answer readable {read}/{len(rows)}")
