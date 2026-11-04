"""Deterministic scoring of one context-selection case. No model judge. Two case sources feed the one metric.

File cases (``cases.json``): is the labelled file where it should be in the returned ranking? ``top1`` = first file, ``top3`` = within the
first 3, ``ok`` = within the solution's declared ``budget`` (3 unless it sets ``select.budget``, as the hybrid union does).

Topic cases (``topic_cases.json``, from QAR's git history, see ``topic_cases.py``): the request names no file and a change needed several.
The case passes when at least half of the needed files are among the first ``TOPIC_VIEW`` files ranked, and only for an arm whose store
holds at most ``TOPIC_STORE_CAP`` cards per file of the corpus: a card per file is a file index, not a topic, so a store of that kind
passes no topic case.

Both sources are cases of the one score: ``[balance.context_selection]`` in esoi.toml folds accuracy over all cases, cost (with the store
charge of ``store_cost.py``) and time into the one balanced number every arm is ranked by; the top-1 / top-3 and topic lines below are detail.

The corpus is the repo checkout itself (read-only; override with ``QAR_EVAL_CORPUS_ROOT``; add roots, such as the sibling hub package, with
``QAR_EVAL_CORPUS_EXTRA_ROOTS``). It has grown since the labels were written, so absolute numbers shift with the tree; compare
implementations to each other on the same checkout."""
import json
from pathlib import Path

import common
from context_selection import topic_cases
from context_selection.corpus import configured_corpus
from context_selection.interface import TOP_K

TOPIC_VIEW = 10
TOPIC_SHARE = 0.5
TOPIC_STORE_CAP = 0.25
HERE = Path(__file__).resolve().parent


def load_cases():
    """The file cases and the pinned topic cases, in that order."""
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8")) + topic_cases.load()


def score_topic(fn, case, corpus, ranked):
    needed = case["needed"]
    share = len(set(needed) & set(ranked[:TOPIC_VIEW])) / len(needed)
    per_file = getattr(fn, "cards_per_file", None)
    per_file = len(corpus.cards) / max(len(corpus.files), 1) if per_file is None else per_file
    gated = per_file > TOPIC_STORE_CAP
    return dict(ok=share >= TOPIC_SHARE and not gated, top1=False, top3=False, topic=True, share=round(share, 3), gated=gated,
                cards_per_file=round(per_file, 3), rank=None, got=f"{round(share * len(needed))}/{len(needed)} needed in the first {TOPIC_VIEW}",
                note=f"{len(needed)} needed" + (f"; store of {per_file:.2f} cards per file is over {TOPIC_STORE_CAP}" if gated else ""))


def score_case(fn, case):
    corpus = configured_corpus(common.REPO)
    ranked = fn(case["task"], corpus)
    if "needed" in case:
        return score_topic(fn, case, corpus, ranked)
    target, budget = case["target"], getattr(fn, "budget", TOP_K)
    rank = ranked.index(target) + 1 if target in ranked else None
    return dict(ok=rank is not None and rank <= budget, top1=rank == 1, top3=rank is not None and rank <= TOP_K,
                rank=rank, got=f"rank {rank}" if rank else "miss", budget=budget,
                note=f"{case['target']} <- {ranked[0] if ranked else 'nothing'}")


def aggregate(rows):
    files = [r for r in rows if not r.get("topic")]
    topics = [r for r in rows if r.get("topic")]
    n = len(files) or 1
    t1, t3, ok = (sum(bool(r.get(k)) for r in files) for k in ("top1", "top3", "ok"))
    budget = max((r.get("budget", TOP_K) for r in files), default=TOP_K)
    extra = f", recall within {budget} files {ok}/{len(files)} ({ok / n:.0%})" if budget != TOP_K else ""
    line = f"top-1 {t1}/{len(files)} ({t1 / n:.0%}), top-3 {t3}/{len(files)} ({t3 / n:.0%}){extra}"
    if topics:
        passed = sum(bool(r["ok"]) for r in topics)
        line += f" | topics {passed}/{len(topics)} ({passed / len(topics):.0%}), {topics[0]['cards_per_file']} cards per file" \
                + (", store over the cap" if topics[0]["gated"] else "")
    return line
