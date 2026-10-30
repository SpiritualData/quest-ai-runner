"""Offline tests for the specificity signal (hubofhubs.critique.specificity, unit-tested in the hub) and its wiring into the
VectorContextAssembler re-rank.

The failure being guarded: a query about one specific subject ("result-prediction evaluation")
must not be led by, or answered from, a sibling that only shares the category ("atom evaluation").
Specificity is measured as neighborhood-IDF: the shared category term is discounted, the rare
distinguishing terms carry the referent. Fully offline, no model, no network.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from hubofhubs.ports.context import VectorHit
from hubofhubs.ports.context import VectorStoreBase
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler


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
