"""Deterministic scoring of one context-selection case: is the labelled file where it should be in the
returned ranking? ``top1`` = first file, ``top3`` = within the first 3, ``ok`` = within the solution's
declared ``budget`` (3 unless it sets ``select.budget``, as the hybrid union does). No model judge.

The corpus is the repo checkout itself (read-only; override with ``QAR_EVAL_CORPUS_ROOT``). It has grown
since the labels were written, so absolute numbers shift with the tree; compare implementations to each
other on the same checkout."""
import os

import common
from context_selection.corpus import corpus_for
from context_selection.interface import TOP_K


def score_case(fn, case):
    corpus = corpus_for(os.environ.get("QAR_EVAL_CORPUS_ROOT") or common.REPO)
    ranked = fn(case["task"], corpus)
    target, budget = case["target"], getattr(fn, "budget", TOP_K)
    rank = ranked.index(target) + 1 if target in ranked else None
    return dict(ok=rank is not None and rank <= budget, top1=rank == 1, top3=rank is not None and rank <= TOP_K,
                rank=rank, got=f"rank {rank}" if rank else "miss", budget=budget,
                note=f"{case['target']} <- {ranked[0] if ranked else 'nothing'}")


def aggregate(rows):
    n = len(rows) or 1
    t1, t3, ok = (sum(bool(r.get(k)) for r in rows) for k in ("top1", "top3", "ok"))
    budget = max((r.get("budget", TOP_K) for r in rows), default=TOP_K)
    extra = f", recall within {budget} files {ok}/{len(rows)} ({ok / n:.0%})" if budget != TOP_K else ""
    return f"top-1 {t1}/{len(rows)} ({t1 / n:.0%}), top-3 {t3}/{len(rows)} ({t3 / n:.0%}){extra}"
