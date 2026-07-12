"""Offline tests for the specificity signal (adapters/specificity.py) and its wiring into the
VectorContextAssembler re-rank.

The failure being guarded: a query about one specific subject ("result-prediction evaluation")
must not be led by, or answered from, a sibling that only shares the category ("atom evaluation").
Specificity is measured as neighborhood-IDF: the shared category term is discounted, the rare
distinguishing terms carry the referent. Fully offline, no model, no network.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import VectorHit, VectorStoreBase
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.adapters import specificity as sp


# ---------------------------------------------------------------------------
# Unit: the scoring model
# ---------------------------------------------------------------------------

def test_shared_category_term_is_discounted_and_rare_term_carries_specificity():
    # Neighborhood of three "evaluation" docs; only one is about "result prediction".
    cands = [
        "result prediction evaluation truth score methodology",  # the real subject
        "atom evaluation pipeline concept framework",            # sibling A
        "habit evaluation streak scoring",                       # sibling B
    ]
    results = sp.score_candidates("what is next for result prediction evaluation", cands)

    real, sib_a, sib_b = results
    # The on-subject doc scores high and names the distinguishing terms it covered.
    assert real.is_specific and real.score > 0.7
    assert "result" in real.matched or "prediction" in real.matched
    # The siblings share only the category ("evaluation") and miss the distinguishing terms.
    assert not sib_a.is_specific and not sib_b.is_specific
    assert "result" in sib_a.missing or "prediction" in sib_a.missing
    # "evaluation" is common to the whole neighborhood, so it must NOT be treated as distinguishing.
    weights = sp.discriminating_weights([sp.tokenize(c) for c in cands])
    assert weights["evaluation"] < weights["result"]
    assert weights["evaluation"] < weights["prediction"]


def test_neutral_when_no_discriminating_structure():
    # Every candidate shares the same terms: nothing to distinguish on -> neutral, never penalized.
    cands = ["quarterly report status", "quarterly report status", "quarterly report status"]
    results = sp.score_candidates("quarterly report status", cands)
    for r in results:
        assert not r.informative
        assert r.score == 1.0
        assert sp.rerank_factor(r) == 1.0  # neutral factor: ranking unchanged vs no-specificity


def test_rerank_factor_demotes_but_never_zeroes():
    weak = sp.SpecificityResult(score=0.0, informative=True)
    strong = sp.SpecificityResult(score=1.0, informative=True)
    assert sp.rerank_factor(weak) == 0.3        # floored, not zeroed (never-worse)
    assert sp.rerank_factor(strong) == 1.0
    assert sp.rerank_factor(weak) < sp.rerank_factor(strong)


def test_empty_query_is_neutral():
    results = sp.score_candidates("", ["anything here", "and here"])
    assert all(not r.informative and r.score == 1.0 for r in results)


# ---------------------------------------------------------------------------
# Integration: the assembler re-ranks on specificity even when raw score favors the sibling
# ---------------------------------------------------------------------------

class FixedScoreStore(VectorStoreBase):
    """A VectorStore that returns a FIXED set of hits with caller-controlled raw scores, so a test
    can prove the specificity re-rank overrides a higher RAW similarity on the wrong subject."""

    def __init__(self, hits: List[VectorHit]) -> None:
        self._hits = hits

    def search(self, query: str, *, scope: Optional[Dict[str, Any]] = None,
               top_k: int = 8) -> List[VectorHit]:
        return list(self._hits)[:top_k]

    def upsert(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> None:
        pass

    def sync(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> int:
        return 0


def test_assembler_prefers_specific_subject_over_higher_scoring_sibling():
    # The sibling ("atom evaluation") has a HIGHER raw similarity than the real subject
    # ("result prediction evaluation"). Specificity must flip the SELECTION ranking (captured as
    # effective_score) and flag the sibling, even though the RENDERED order is stable by card id
    # (a prefix-cache precondition, see vector_context_assembler.py) and so does not itself move.
    hits = [
        VectorHit(
            id="atom-eval",
            score=0.90,  # higher raw similarity -- the failure mode
            text="Atom evaluation pipeline report: concept framework and next steps",
            payload={"summary": "atom evaluation pipeline status and next steps"},
        ),
        VectorHit(
            id="result-pred-eval",
            score=0.70,  # lower raw similarity, but the actual subject asked about
            text="Result prediction evaluation: truth score methodology and next steps",
            payload={"summary": "result prediction evaluation methodology and next steps"},
        ),
    ]
    assembler = VectorContextAssembler(
        FixedScoreStore(hits),
        provider=None,            # no LLM steps: prove the model-free signal alone
        confidence_min_score=0.0,
        num_queries=0,
    )
    ctx = assembler.assemble("what is next for result prediction evaluation")

    view = ctx.context_view
    by_id = {c["id"]: c for c in ctx.card_metadata}
    # 1. The specific subject now ranks higher by effective_score despite its lower raw score
    # (SELECTION signal). The RENDERED order in context_view/card_ids is stable by card id
    # ("atom-eval" < "result-pred-eval"), independent of this ranking.
    assert by_id["result-pred-eval"]["effective_score"] > by_id["atom-eval"]["effective_score"]
    assert ctx.card_ids == ["atom-eval", "result-pred-eval"]
    # 2. The sibling is explicitly flagged as a weak / adjacent match in the text the LLM reads.
    atom_section = view.split("### Vector hit: atom-eval")[1]
    assert "WEAK" in atom_section and "ADJACENT" in atom_section
    assert "result" in atom_section or "prediction" in atom_section  # names the missing terms
    # 3. The real subject is labeled on-subject.
    assert "subject match: on-subject" in view

    # 4. The structured signal is on card_metadata for consumers/UI.
    assert by_id["result-pred-eval"]["specificity"]["on_subject"] is True
    assert by_id["atom-eval"]["specificity"]["on_subject"] is False
    assert by_id["result-pred-eval"]["specificity"]["score"] > by_id["atom-eval"]["specificity"]["score"]
