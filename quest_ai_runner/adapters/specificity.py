"""specificity -- efficient, model-free scoring of whether a retrieved candidate matches the
SPECIFIC subject of a query, not merely its category.

THE FAILURE THIS ADDRESSES
--------------------------
Dense retrieval ranks by topical similarity, so a query about one specific subject
("result-prediction evaluation") pulls back siblings that share the CATEGORY head ("evaluation")
but are a DIFFERENT specific thing ("atom evaluation"). The category term is common across the
whole retrieved neighborhood; the DISCRIMINATING terms ("result-prediction", "atom") are what pin
the referent. A bi-encoder averages them together, so the category swamps the modifier and a
sibling scores nearly as high as the real match.

THE PRINCIPLE
-------------
Specificity lives in the query terms that PARTITION the retrieved neighborhood: terms that some
candidates have and others do not. We weight each term by its power to split the candidate set,
``p * (1 - p)`` where ``p = df / N`` is the fraction of candidates containing it (Bernoulli
variance / Gini information). This peaks for a term in about half the candidates and falls to zero
at both extremes:

  * a term in EVERY candidate (the shared category head, e.g. "evaluation" across a neighborhood of
    evaluation docs) has ``p = 1`` -> weight 0. It cannot distinguish the referent.
  * a generic ASK word absent from every candidate (e.g. "next" in "what's next for X") has
    ``p = 0`` -> weight 0. Nothing retrieved covers it, so it cannot rank the candidates against
    each other either.

What remains and carries weight are the distinguishing terms of the actual subject
("result-prediction" vs "atom"). A candidate's specificity score is the fraction of that
distinguishing MASS it covers. This is information-theoretic, not a keyword heuristic: no model
call, no global corpus, no word list, no extra dependency. It self-identifies the category head AND
the generic ask-words and discounts both, with nothing hand-maintained.

BOUNDARY (honest): the signal distinguishes AMONG the retrieved candidates. When the true subject
was never retrieved at all (every candidate is a sibling), there is nothing in the neighborhood to
partition on, so the signal goes NEUTRAL rather than guessing. That case is left to the similarity
floor and the prompt-level specificity gate; a later cross-encoder/NLI increment (query-vs-doc,
not neighborhood-relative) is what would flag it.

TWO OUTPUTS, BOTH CHEAP
-----------------------
  * ``score`` in [0, 1] -- used to RE-RANK (as the primary key, ahead of recency) and to FLAG weak
    matches. It re-orders and labels; it does NOT gate (never-worse: a still-similar hit is never
    dropped by this signal, only pushed down and annotated). The similarity floor stays the gate.
  * ``matched`` / ``missing`` distinguishing terms -- surfaced as a per-source label so the LLM
    reads a grounded "on-subject vs adjacent" signal instead of inferring it from vibes.

When a query has no discriminating structure (all terms equally common, or no content terms) the
signal is NEUTRAL (score 1.0, nothing flagged): ranking falls back to similarity + recency and
nothing is penalized. So adding this layer never makes a run worse than running without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set

from .card_content_render import tokenize

# A query term counts as "distinguishing" (named in the label, and the basis of the on-subject
# judgement) when its neighborhood-IDF weight is at least this fraction of the query's TOP weight.
# Relative, not absolute, so it adapts to each neighborhood: the category head is always the
# low-weight term and always falls below the cut, whatever the corpus.
_DISTINCTIVE_FRACTION = 0.5

# Below this specificity score a candidate is flagged as a likely ADJACENT (sibling) topic rather
# than the specific subject asked about. Tuned to fire when a candidate covers the category but
# misses the distinguishing terms, while a candidate that covers them clears it comfortably.
WEAK_MATCH_THRESHOLD = 0.5


@dataclass
class SpecificityResult:
    """Per-candidate specificity signal.

    score:     fraction of the query's discriminating mass the candidate covers, in [0, 1].
    matched:   distinguishing query terms the candidate DOES contain (ordered by weight).
    missing:   distinguishing query terms the candidate does NOT contain (ordered by weight).
    is_specific:  score >= WEAK_MATCH_THRESHOLD (a positive on-subject match).
    informative:  whether the query had any discriminating structure to judge on. When False the
                  score is a neutral 1.0 and callers should not flag or penalize the candidate.
    """

    score: float = 1.0
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    is_specific: bool = True
    informative: bool = False


def discriminating_weights(candidate_term_sets: Sequence[Set[str]]) -> Dict[str, float]:
    """Neighborhood partition-power weight for every term across the retrieved candidate set.

    ``weight(term) = p * (1 - p)`` where ``p = df / N``, ``df`` = candidates containing the term,
    ``N`` = candidate count. Peaks at ``p = 0.5`` (a term that splits the neighborhood); zero at
    ``p = 1`` (in every candidate: the shared category head) and effectively unreachable at
    ``p = 0`` (terms absent from every candidate never enter this dict). Never negative. Never
    raises.
    """
    n = len(candidate_term_sets)
    if n == 0:
        return {}
    df: Dict[str, int] = {}
    for terms in candidate_term_sets:
        for t in terms:
            df[t] = df.get(t, 0) + 1
    weights: Dict[str, float] = {}
    for t, d in df.items():
        p = d / n
        weights[t] = p * (1.0 - p)
    return weights


def score_candidate(
    query_terms: Set[str],
    candidate_terms: Set[str],
    neighborhood: Dict[str, float],
) -> SpecificityResult:
    """Score ONE candidate's specificity against ``query_terms`` given the precomputed
    neighborhood weights. See module docstring for the model. Never raises.

    A query term absent from every candidate (df=0) is not in ``neighborhood`` and gets weight 0:
    it can neither be the shared category nor partition the candidates, so it does not count."""
    if not query_terms:
        return SpecificityResult(score=1.0, informative=False)

    q_weights = {t: neighborhood.get(t, 0.0) for t in query_terms}
    total = sum(q_weights.values())
    if total <= 1e-9:
        # No discriminating structure (e.g. every term shared across the neighborhood): neutral.
        return SpecificityResult(score=1.0, informative=False)

    covered = sum(w for t, w in q_weights.items() if t in candidate_terms)
    score = max(0.0, min(1.0, covered / total))

    # Name the distinguishing terms (top-weighted query terms) for the human-readable label.
    top = max(q_weights.values())
    cut = _DISTINCTIVE_FRACTION * top
    distinctive = sorted(
        (t for t, w in q_weights.items() if w >= cut and w > 0.0),
        key=lambda t: q_weights[t],
        reverse=True,
    )
    matched = [t for t in distinctive if t in candidate_terms]
    missing = [t for t in distinctive if t not in candidate_terms]
    return SpecificityResult(
        score=score,
        matched=matched,
        missing=missing,
        is_specific=score >= WEAK_MATCH_THRESHOLD,
        informative=True,
    )


def score_candidates(query_text: str, candidate_texts: Sequence[str]) -> List[SpecificityResult]:
    """Score every candidate's specificity against the query in one pass.

    ``candidate_texts`` is the searchable text of each retrieved candidate (title/summary/snippet/
    paths concatenated). Returns a list aligned with ``candidate_texts``. Never raises: on any error
    it returns neutral results so the caller's ranking is unchanged (never-worse).
    """
    try:
        query_terms = tokenize(query_text or "")
        cand_term_sets = [tokenize(t or "") for t in candidate_texts]
        neighborhood = discriminating_weights(cand_term_sets)
        return [
            score_candidate(query_terms, cand_terms, neighborhood)
            for cand_terms in cand_term_sets
        ]
    except Exception:  # noqa: BLE001 -- scoring must never break retrieval
        return [SpecificityResult(score=1.0, informative=False) for _ in candidate_texts]


def rerank_factor(result: SpecificityResult, floor: float = 0.3) -> float:
    """Map a specificity score to a multiplicative re-rank factor in ``[floor, 1.0]``.

    ``floor`` keeps the signal NEVER-WORSE: a zero-specificity sibling is pushed DOWN but never
    zeroed out, so if nothing better was retrieved it can still surface (the similarity floor, not
    this factor, is what gates). A full-specificity match keeps its full weight. A non-informative
    result maps to 1.0 (no effect)."""
    if not result.informative:
        return 1.0
    return floor + (1.0 - floor) * max(0.0, min(1.0, result.score))
