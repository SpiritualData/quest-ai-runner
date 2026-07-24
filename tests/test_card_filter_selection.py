"""Offline tests for ``filter_cards_by_relevance``: the batched file ranking + the selection memo.

Covers the two call-reduction changes in ``core/card_filter.py``:

* Stage 2 (within-card file ranking) is ONE batched LLM call over ALL selected cards, never one
  call per card; a malformed ranking response degrades to each card's original file order.
* The selection memo is PER-PROVIDER (a ``WeakKeyDictionary`` of bounded LRUs): a repeat ask with
  identical inputs skips the LLM calls and returns copies of the prior verdict; ANY changed input
  (card content, topic keywords, model, provider instance) misses; fallback verdicts (no provider,
  stage-1 failure) are byte-identical and never cached; a provider's entries die with the provider
  so a NEW provider always starts cold (the ``str(id(provider))`` key bug regression).
"""
from __future__ import annotations

import gc
from typing import Any, Dict, List

from quest_ai_runner.core.card_filter import (
    consolidate_context,
    filter_cards_by_relevance,
)


class _CountingProvider:
    """A ModelProvider stub replaying scripted answers in order (last one repeats)."""

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.answer_calls = 0
        self.prompts: List[str] = []

    def plan(self, prompt, *, model, tool_schema):  # pragma: no cover - unused here
        return {"action": "answer"}

    def answer(self, messages, *, model=None, system=None) -> str:
        self.answer_calls += 1
        self.prompts.append("\n".join(m["content"] for m in messages))
        resp = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(resp, Exception):
            raise resp
        return resp

    def list_models(self) -> List[str]:
        return ["stub-model"]


_STAGE1_KEEP_ALL = (
    '{"cards": [{"id": "card-a", "score": 0.9}, {"id": "card-b", "score": 0.8}, '
    '{"id": "card-c", "score": 0.7}]}'
)

_STAGE2_RANKING = (
    '{"cards": ['
    '{"card_id": "card-a", "files": ['
    '{"path": "a3.py", "score": 0.9}, {"path": "a1.py", "score": 0.5}, '
    '{"path": "a2.py", "score": 0.1}]},'
    '{"card_id": "card-b", "files": ['
    '{"path": "b2.py", "score": 1.0}, {"path": "b1.py", "score": 0.2}]}'
    ']}'
)


def _cards() -> List[Dict[str, Any]]:
    """Three candidates: two with files (need ranking), one file-less."""
    return [
        {"id": "card-a", "title": "Billing", "files": ["a1.py", "a2.py", "a3.py"],
         "adapter": "keyword"},
        {"id": "card-b", "title": "Payments", "files": ["b1.py", "b2.py"],
         "adapter": "keyword"},
        {"id": "card-c", "title": "Notes", "files": [], "adapter": "keyword"},
    ]


# ---------------------------------------------------------------------------
# Batched file ranking (stage 2)
# ---------------------------------------------------------------------------

class TestBatchedFileRanking:
    def test_one_provider_call_ranks_files_for_all_cards(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        out = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        # Exactly TWO calls total: one card-level scoring + ONE batched ranking, never one
        # ranking call per card.
        assert prov.answer_calls == 2
        by_id = {m.id: m for m in out}
        assert by_id["card-a"].files == ["a3.py", "a1.py", "a2.py"]
        assert by_id["card-b"].files == ["b2.py", "b1.py"]
        assert by_id["card-c"].files == []
        # The single ranking prompt names BOTH file-bearing cards.
        assert "[card-a]" in prov.prompts[1] and "[card-b]" in prov.prompts[1]

    def test_malformed_ranking_response_degrades_to_original_order(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, "this is not json at all"])
        out = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        assert prov.answer_calls == 2
        by_id = {m.id: m for m in out}
        # Original order, top 5 (the same fallback the old per-card loop had).
        assert by_id["card-a"].files == ["a1.py", "a2.py", "a3.py"]
        assert by_id["card-b"].files == ["b1.py", "b2.py"]

    def test_non_numeric_scores_are_coerced_and_never_kill_the_selection(self):
        # Regression: the model is free to return a score of any JSON shape. A "0.9" string, a
        # null, or a word used to land in the score map untouched, and the sort compared it
        # against the numeric default -> TypeError. That raise escaped ``_rank_files_batched``
        # and ``filter_cards_by_relevance`` entirely, so each caller's blanket except threw away
        # the whole card-level LLM selection, not just the file ranking.
        ranking = (
            '{"cards": ['
            '{"card_id": "card-a", "files": ['
            '{"path": "a1.py", "score": 0.1}, {"path": "a2.py", "score": null}, '
            '{"path": "a3.py", "score": "0.9"}]},'
            '{"card_id": "card-b", "files": ['
            '{"path": "b1.py", "score": "very relevant"}, {"path": "b2.py", "score": 1.0}]}'
            ']}'
        )
        prov = _CountingProvider([_STAGE1_KEEP_ALL, ranking])
        out = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        # The card-level selection survives in full.
        assert [m.id for m in out] == ["card-a", "card-b", "card-c"]
        by_id = {m.id: m for m in out}
        # "0.9" parses as 0.9; null and "very relevant" fall back to the 0.5 neutral score.
        assert by_id["card-a"].files == ["a3.py", "a2.py", "a1.py"]
        assert by_id["card-b"].files == ["b2.py", "b1.py"]

    def test_ranking_call_uses_the_caller_resolved_model(self):
        captured: List[Any] = []

        class _ModelCapture(_CountingProvider):
            def answer(self, messages, *, model=None, system=None) -> str:
                captured.append(model)
                return super().answer(messages, model=model, system=system)

        prov = _ModelCapture([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance(
            "billing task", _cards(), model_provider=prov, model="cheap-tier")
        # Both stage 1 and the batched stage 2 use the caller-resolved tier, never a silent
        # provider default.
        assert captured == ["cheap-tier", "cheap-tier"]


# ---------------------------------------------------------------------------
# Selection memo
# ---------------------------------------------------------------------------

class TestSelectionMemo:
    def test_memo_hit_on_identical_inputs_skips_all_llm_calls(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        first = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        assert prov.answer_calls == 2
        second = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        assert prov.answer_calls == 2  # zero additional calls
        assert second == first

    def test_memo_hit_on_keyword_equivalent_paraphrase(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        # Same keyword SET (order/case/punctuation differ) -> same memo entry.
        filter_cards_by_relevance("Task: BILLING!", _cards(), model_provider=prov)
        assert prov.answer_calls == 2

    def test_cached_verdict_is_returned_as_copies(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        first = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        first[0].files.append("mutated.py")
        second = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        assert "mutated.py" not in second[0].files

    def test_memo_miss_on_changed_card_content(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        changed = _cards()
        changed[0]["files"] = ["a1.py", "a2.py", "a3.py", "a4.py"]
        filter_cards_by_relevance("billing task", changed, model_provider=prov)
        assert prov.answer_calls == 4

    def test_memo_miss_on_changed_topic_keywords(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        filter_cards_by_relevance("theme colors", _cards(), model_provider=prov)
        assert prov.answer_calls == 4

    def test_memo_miss_on_changed_model(self):
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov, model="m1")
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov, model="m2")
        assert prov.answer_calls == 4

    def test_memo_miss_on_new_provider_with_identical_inputs(self):
        # The provider is the memo's WeakKeyDictionary key, so a DIFFERENT provider instance
        # never shares a verdict even with byte-identical inputs.
        prov_a = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        prov_b = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov_a)
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov_b)
        assert prov_a.answer_calls == 2
        assert prov_b.answer_calls == 2

    def test_dead_provider_verdict_never_serves_a_new_provider(self):
        # Regression for the str(id(provider)) memo key: id() values are reused after garbage
        # collection, so a dead provider's cached verdict could be served to a NEW provider with
        # identical inputs. With the per-provider WeakKeyDictionary the dead provider's entries
        # die with it, so the new provider's own (different) verdict must be returned.
        prov_a = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov_a)
        del prov_a
        gc.collect()
        keep_only_b = '{"cards": [{"id": "card-b", "score": 0.9}]}'
        prov_b = _CountingProvider([keep_only_b, _STAGE2_RANKING])
        out = filter_cards_by_relevance("billing task", _cards(), model_provider=prov_b)
        assert prov_b.answer_calls == 2
        assert [m.id for m in out] == ["card-b"]

    def test_stage1_failure_fallback_is_never_cached(self):
        prov = _CountingProvider([RuntimeError("boom")])
        first = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        calls_after_first = prov.answer_calls
        second = filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        # The fallback verdict is recomputed (fresh provider calls), never pinned in the memo.
        assert prov.answer_calls > calls_after_first
        assert second == first  # neutral-score fallback is still deterministic

    def test_consolidate_memo_is_per_provider_too(self):
        cards = [{
            "id": "card-a", "title": "Billing",
            "items": [{"id": "a1", "type": "file", "why": "entry", "preview": "def run()"}],
        }]
        verdict = '{"cards": [{"card_id": "card-a", "items": [{"item_id": "a1", "deliver": "paste"}]}]}'
        prov_a = _CountingProvider([verdict])
        consolidate_context("task", cards, model_provider=prov_a)
        consolidate_context("task", cards, model_provider=prov_a)
        assert prov_a.answer_calls == 1  # second ask memo-hit
        prov_b = _CountingProvider([verdict])
        consolidate_context("task", cards, model_provider=prov_b)
        assert prov_b.answer_calls == 1  # new provider starts cold


# ---------------------------------------------------------------------------
# No-provider fallback: byte-identical and never cached
# ---------------------------------------------------------------------------

class TestNoProviderFallback:
    def test_no_provider_fallback_is_byte_identical_and_uncached(self):
        first = filter_cards_by_relevance("billing task", _cards(), model_provider=None)
        second = filter_cards_by_relevance("billing task", _cards(), model_provider=None)
        assert second == first
        assert [m.id for m in first] == ["card-a", "card-b", "card-c"]
        assert all(m.relevance_score == 0.7 for m in first)
        by_id = {m.id: m for m in first}
        assert by_id["card-a"].files == ["a1.py", "a2.py", "a3.py"]  # top 3, original order
        # Never cached: the SAME inputs with a provider wired go straight to the LLM.
        prov = _CountingProvider([_STAGE1_KEEP_ALL, _STAGE2_RANKING])
        filter_cards_by_relevance("billing task", _cards(), model_provider=prov)
        assert prov.answer_calls == 2
