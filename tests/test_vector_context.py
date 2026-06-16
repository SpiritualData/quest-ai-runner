"""Offline tests for the vector context layer.

Tests are fully offline: no real Qdrant, no fastembed, no network.
A FAKE in-memory VectorStore is used for all core tests.
The QdrantVectorStore smoke test is guarded by pytest.importorskip so it is
skipped when qdrant-client is not installed.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from quest_ai_runner.core.adapters import (
    AssembledContext,
    ContextAssembler,
    VectorHit,
    VectorStore,
    VectorStoreBase,
)
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler


# ---------------------------------------------------------------------------
# Fake in-memory VectorStore for tests
# ---------------------------------------------------------------------------

class FakeVectorStore(VectorStoreBase):
    """In-memory VectorStore that satisfies the VectorStore Protocol.

    Uses simple substring matching (not real embeddings) so tests are
    deterministic and dependency-free.

    Also implements the optional capacity methods ``count`` and ``evict_oldest``
    so tests for the capacity bound can use a real in-memory store.
    """

    def __init__(self) -> None:
        # {collection: {id: {text, payload, fingerprint}}}
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _coll(self, scope: Optional[Dict[str, Any]]) -> str:
        if not scope:
            return "_default"
        import hashlib
        parts = sorted(f"{k}={v}" for k, v in scope.items())
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]

    def search(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        top_k: int = 8,
    ) -> List[VectorHit]:
        try:
            coll = self._coll(scope)
            items = self._data.get(coll, {})
            hits = []
            q_lower = query.lower()
            for item_id, item in items.items():
                text = item.get("text", "")
                # Score = number of query words found in text (simple overlap).
                words = set(q_lower.split())
                text_lower = text.lower()
                score = sum(1.0 for w in words if w in text_lower) / max(len(words), 1)
                if score > 0:
                    hits.append(
                        VectorHit(
                            id=item_id,
                            score=score,
                            text=text,
                            payload=dict(item.get("payload") or {}),
                        )
                    )
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:top_k]
        except Exception:
            return []

    def upsert(
        self,
        items: List[Dict[str, Any]],
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            coll = self._coll(scope)
            if coll not in self._data:
                self._data[coll] = {}
            for item in items:
                self._data[coll][item["id"]] = {
                    "text": item.get("text", ""),
                    "payload": item.get("payload") or {},
                    "fingerprint": item.get("fingerprint"),
                }
        except Exception:
            pass

    def sync(
        self,
        items: List[Dict[str, Any]],
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> int:
        try:
            coll = self._coll(scope)
            stored = self._data.get(coll, {})
            to_upsert = []
            for item in items:
                stored_item = stored.get(item["id"])
                new_fp = item.get("fingerprint")
                old_fp = stored_item.get("fingerprint") if stored_item else None
                if stored_item is None or new_fp != old_fp:
                    to_upsert.append(item)
            if to_upsert:
                self.upsert(to_upsert, scope=scope)
            return len(to_upsert)
        except Exception:
            return 0

    # --- Optional capacity methods -------------------------------------------

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        """Return the number of stored associations for the given scope."""
        try:
            coll = self._coll(scope)
            return len(self._data.get(coll, {}))
        except Exception:
            return 0

    def evict_oldest(
        self,
        n: int,
        *,
        scope: Optional[Dict[str, Any]] = None,
        ts_key: str = "ts",
    ) -> int:
        """Delete the ``n`` oldest points (sorted by ``ts_key`` payload field, asc)."""
        try:
            if n <= 0:
                return 0
            coll = self._coll(scope)
            items = self._data.get(coll, {})
            if not items:
                return 0
            # Sort by ts ascending (missing ts treated as 0).
            sorted_ids = sorted(
                items.keys(),
                key=lambda k: float(items[k].get("payload", {}).get(ts_key, 0) or 0),
            )
            to_delete = sorted_ids[:n]
            for k in to_delete:
                del items[k]
            return len(to_delete)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(
    query_responses: Optional[List[str]] = None,
    review_response: Optional[str] = None,
) -> MagicMock:
    """Build a mock ModelProvider.

    ``query_responses`` — list of ``answer()`` return values in call order.
    ``review_response`` — if given, ``answer()`` always returns this for the
    review step.  Otherwise combined with query_responses in order.
    """
    provider = MagicMock()
    provider.list_models.return_value = []
    provider.plan.return_value = {"action": "answer", "rationale": "ok", "model_tier": "haiku"}

    all_responses = list(query_responses or []) + (
        [review_response] if review_response else []
    )
    if all_responses:
        provider.answer.side_effect = all_responses
    else:
        provider.answer.return_value = ""
    return provider


# ---------------------------------------------------------------------------
# VectorHit dataclass
# ---------------------------------------------------------------------------

class TestVectorHit:
    def test_defaults(self):
        h = VectorHit(id="x", score=0.9)
        assert h.id == "x"
        assert h.score == 0.9
        assert h.text == ""
        assert h.payload == {}

    def test_fields(self):
        h = VectorHit(id="a", score=0.5, text="hello", payload={"k": "v"})
        assert h.text == "hello"
        assert h.payload == {"k": "v"}


# ---------------------------------------------------------------------------
# VectorStore Protocol structural check
# ---------------------------------------------------------------------------

class TestVectorStoreProtocol:
    def test_fake_store_satisfies_protocol(self):
        store = FakeVectorStore()
        assert isinstance(store, VectorStore)

    def test_minimal_structural_satisfaction(self):
        class Minimal:
            def search(self, query, *, scope=None, top_k=8):
                return []
            def upsert(self, items, *, scope=None):
                pass
            def sync(self, items, *, scope=None):
                return 0

        assert isinstance(Minimal(), VectorStore)

    def test_missing_method_fails_protocol(self):
        class Bad:
            def search(self, query, *, scope=None, top_k=8):
                return []
            def upsert(self, items, *, scope=None):
                pass
            # missing sync

        assert not isinstance(Bad(), VectorStore)


# ---------------------------------------------------------------------------
# FakeVectorStore: search / upsert / sync
# ---------------------------------------------------------------------------

class TestFakeVectorStore:
    def test_search_empty_store_returns_empty(self):
        store = FakeVectorStore()
        assert store.search("billing payment") == []

    def test_upsert_then_search_finds_hit(self):
        store = FakeVectorStore()
        store.upsert([{"id": "doc1", "text": "billing payment pipeline"}])
        hits = store.search("billing payment")
        assert len(hits) == 1
        assert hits[0].id == "doc1"

    def test_search_score_orders_by_overlap(self):
        store = FakeVectorStore()
        store.upsert([
            {"id": "high", "text": "billing payment pipeline collate"},
            {"id": "low", "text": "billing unrelated"},
        ])
        hits = store.search("billing payment pipeline collate")
        assert hits[0].id == "high"

    def test_scope_isolation(self):
        store = FakeVectorStore()
        store.upsert([{"id": "scoped", "text": "billing"}], scope={"team": "A"})
        # Search with a different scope returns nothing.
        hits = store.search("billing", scope={"team": "B"})
        assert hits == []
        # Search with the right scope finds it.
        hits = store.search("billing", scope={"team": "A"})
        assert len(hits) == 1


# ---------------------------------------------------------------------------
# AUTO-UPDATE: sync re-embeds only changed items
# ---------------------------------------------------------------------------

class TestSync:
    def test_sync_upserts_new_items(self):
        store = FakeVectorStore()
        count = store.sync([{"id": "item1", "text": "hello", "fingerprint": "fp1"}])
        assert count == 1
        hits = store.search("hello")
        assert any(h.id == "item1" for h in hits)

    def test_sync_skips_unchanged_items(self):
        store = FakeVectorStore()
        # Initial upsert.
        store.sync([{"id": "item1", "text": "hello", "fingerprint": "fp1"}])
        # Sync again with the same fingerprint: count should be 0.
        count = store.sync([{"id": "item1", "text": "hello", "fingerprint": "fp1"}])
        assert count == 0

    def test_sync_re_embeds_changed_fingerprint(self):
        store = FakeVectorStore()
        store.sync([{"id": "item1", "text": "hello", "fingerprint": "fp1"}])
        # Change the fingerprint: must re-embed.
        count = store.sync([{"id": "item1", "text": "hello updated", "fingerprint": "fp2"}])
        assert count == 1

    def test_sync_mixed_changed_and_unchanged(self):
        store = FakeVectorStore()
        store.sync([
            {"id": "a", "text": "alpha", "fingerprint": "fp-a"},
            {"id": "b", "text": "beta", "fingerprint": "fp-b"},
        ])
        # Change only "a".
        count = store.sync([
            {"id": "a", "text": "alpha updated", "fingerprint": "fp-a-new"},
            {"id": "b", "text": "beta", "fingerprint": "fp-b"},
        ])
        assert count == 1

    def test_sync_never_raises_on_empty(self):
        store = FakeVectorStore()
        assert store.sync([]) == 0


# ---------------------------------------------------------------------------
# VectorContextAssembler: no provider (raw query only)
# ---------------------------------------------------------------------------

class TestVectorContextAssemblerNoProvider:
    def test_searches_with_raw_input(self):
        store = FakeVectorStore()
        store.upsert([{"id": "billing-doc", "text": "billing payment collate"}])
        asm = VectorContextAssembler(store)
        ac = asm.assemble("billing payment")
        assert "billing-doc" in ac.card_ids

    def test_returns_empty_when_no_hits(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store)
        ac = asm.assemble("nonexistent topic zzz")
        assert ac.context_view == ""
        assert ac.card_ids == []

    def test_context_view_contains_hit_id(self):
        store = FakeVectorStore()
        store.upsert([{"id": "hit-one", "text": "the quick brown fox"}])
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        ac = asm.assemble("quick fox")
        assert "hit-one" in ac.context_view

    def test_confidence_gate_filters_low_scores(self):
        """With a high confidence threshold, low-scoring hits are gated out."""
        store = FakeVectorStore()
        store.upsert([{"id": "weak", "text": "billing"}])
        # The fake store gives score=1.0/len(query_words) for a single matching word.
        # With a 2-word query only one word matches -> score = 0.5.
        asm = VectorContextAssembler(store, confidence_min_score=0.9)
        ac = asm.assemble("billing unrelated")
        assert ac.context_view == ""

    def test_max_in_view_respected(self):
        store = FakeVectorStore()
        for i in range(10):
            store.upsert([{"id": f"doc-{i}", "text": f"billing item {i}"}])
        asm = VectorContextAssembler(store, max_in_view=3, confidence_min_score=0.0)
        ac = asm.assemble("billing item")
        assert len(ac.card_ids) <= 3

    def test_assemble_never_raises(self):
        class BrokenStore(VectorStoreBase):
            def search(self, q, *, scope=None, top_k=8):
                raise RuntimeError("broken")
            def upsert(self, items, *, scope=None):
                raise RuntimeError("broken")
            def sync(self, items, *, scope=None):
                raise RuntimeError("broken")

        asm = VectorContextAssembler(BrokenStore())
        ac = asm.assemble("any task")
        assert isinstance(ac, AssembledContext)

    def test_scope_from_meta_forwarded(self):
        store = FakeVectorStore()
        store.upsert([{"id": "scoped-hit", "text": "billing"}], scope={"team": "A"})
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        # Without scope: no hit.
        ac_no_scope = asm.assemble("billing")
        # With scope: hit found.
        ac_with_scope = asm.assemble("billing", meta={"team": "A"})
        assert "scoped-hit" not in ac_no_scope.card_ids
        assert "scoped-hit" in ac_with_scope.card_ids


# ---------------------------------------------------------------------------
# VectorContextAssembler: with provider (query-gen + LLM review)
# ---------------------------------------------------------------------------

class TestVectorContextAssemblerWithProvider:
    def test_llm_generates_queries_used_in_parallel_search(self):
        """With a provider, extra queries are generated and searched."""
        store = FakeVectorStore()
        # Put a doc that only matches LLM-generated queries, not the raw task.
        store.upsert([{"id": "semantic-doc", "text": "payment collection"}])

        # Provider: answer() for query-gen returns a useful query; answer() for
        # review returns "0" (select the first candidate).
        provider = MagicMock()
        provider.answer.side_effect = [
            "payment collection pipeline\nfinancial aggregation",  # query gen
            "0",  # review: keep index 0
        ]

        asm = VectorContextAssembler(
            store,
            provider=provider,
            num_queries=2,
            confidence_min_score=0.0,
        )
        ac = asm.assemble("billing collate task")
        # The LLM-generated query "payment collection pipeline" should find semantic-doc.
        assert provider.answer.call_count >= 1

    def test_llm_review_filters_irrelevant_hits(self):
        """The LLM review step removes hits the model marks as not relevant."""
        store = FakeVectorStore()
        store.upsert([
            {"id": "relevant", "text": "billing payment"},
            {"id": "irrelevant", "text": "billing payment"},
        ])

        provider = MagicMock()
        # Query gen: empty (so only raw task is searched).
        # Review: return "0" (only keep the first hit).
        provider.answer.side_effect = [
            "",     # query gen: no extra queries
            "0",    # review: keep only index 0
        ]

        asm = VectorContextAssembler(
            store,
            provider=provider,
            num_queries=0,
            confidence_min_score=0.0,
        )
        ac = asm.assemble("billing payment task")
        # Only one hit should survive the review.
        assert len(ac.card_ids) <= 1

    def test_review_none_response_gives_empty_context(self):
        """When the LLM review says 'none', no context is injected."""
        store = FakeVectorStore()
        store.upsert([{"id": "doc", "text": "billing payment"}])

        provider = MagicMock()
        provider.answer.side_effect = [
            "",      # no extra queries
            "none",  # review: nothing relevant
        ]

        asm = VectorContextAssembler(
            store,
            provider=provider,
            num_queries=0,
            confidence_min_score=0.0,
        )
        ac = asm.assemble("billing payment")
        assert ac.context_view == ""
        assert ac.card_ids == []

    def test_provider_none_skips_llm_steps(self):
        """Without a provider, no LLM calls are made."""
        store = FakeVectorStore()
        store.upsert([{"id": "doc", "text": "billing payment"}])

        asm = VectorContextAssembler(store, provider=None, confidence_min_score=0.0)
        ac = asm.assemble("billing payment")
        # Should find the hit without any LLM calls.
        assert "doc" in ac.card_ids

    def test_review_failure_keeps_all_candidates(self):
        """When review call fails, all candidates above the confidence gate survive."""
        store = FakeVectorStore()
        store.upsert([{"id": "doc", "text": "billing payment"}])

        provider = MagicMock()
        provider.answer.side_effect = [
            "",                       # no extra queries
            RuntimeError("oops"),     # review call fails
        ]

        asm = VectorContextAssembler(
            store,
            provider=provider,
            num_queries=0,
            confidence_min_score=0.0,
        )
        ac = asm.assemble("billing payment")
        # Candidates survive despite review failure.
        assert len(ac.card_ids) >= 1


# ---------------------------------------------------------------------------
# VectorContextAssembler: record() compounds the store
# ---------------------------------------------------------------------------

class TestVectorContextAssemblerRecord:
    def test_record_upserts_into_store(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("billing payment task", {"kind": "met"})
        # After record, searching with the task text should find the upserted point.
        hits = store.search("billing payment")
        assert len(hits) > 0

    def test_record_never_raises(self):
        class BrokenStore(VectorStoreBase):
            def search(self, q, *, scope=None, top_k=8):
                return []
            def upsert(self, items, *, scope=None):
                raise RuntimeError("upsert failed")
            def sync(self, items, *, scope=None):
                return 0

        asm = VectorContextAssembler(BrokenStore())
        asm.record("anything", {"kind": "met"})  # must not raise


# ---------------------------------------------------------------------------
# VectorContextAssembler: enriched record() -- Thing 2
# ---------------------------------------------------------------------------

class TestVectorContextAssemblerRecordEnrichment:
    """Thing 2: record() upserts a rich task-to-context association."""

    def test_record_embeds_task_text(self):
        """The upserted text contains the task text so it is searchable by similar tasks."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("implement the billing collator", {"kind": "met", "files": ["billing/collate.py"]})

        # Searching with the task text should find the upserted item.
        hits = store.search("billing collator")
        assert len(hits) > 0, "upserted task association not found by vector search"

    def test_record_payload_contains_paths(self):
        """The payload must contain the file paths from the outcome."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record(
            "fix the payment collector",
            {
                "kind": "met",
                "files": ["billing/collate.py", "billing/models.py"],
            },
        )

        # Find the upserted item in the store.
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        payload = item.get("payload") or {}
        assert "billing/collate.py" in payload.get("paths", []), (
            f"expected paths in payload, got: {payload}"
        )
        assert "billing/models.py" in payload.get("paths", []), (
            f"expected second path in payload, got: {payload}"
        )

    def test_record_payload_contains_task(self):
        """The payload must carry the original task text."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        task = "refactor the authentication middleware"
        asm.record(task, {"kind": "met"})

        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        payload = item.get("payload") or {}
        assert payload.get("task") == task, (
            f"expected task in payload, got: {payload}"
        )

    def test_record_payload_contains_kind(self):
        """The payload must carry the outcome kind."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("do something", {"kind": "met"})

        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        payload = item.get("payload") or {}
        assert payload.get("kind") == "met", (
            f"expected kind='met' in payload, got: {payload}"
        )

    def test_record_with_symbols_in_payload(self):
        """When outcome carries symbols, they appear in the payload."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record(
            "use PaymentCollector to collate",
            {
                "kind": "met",
                "files": ["billing/collate.py"],
                "symbols": ["PaymentCollector", "xfr_collate"],
            },
        )

        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        payload = item.get("payload") or {}
        assert "PaymentCollector" in payload.get("symbols", []), (
            f"expected symbols in payload, got: {payload}"
        )

    def test_record_with_provider_uses_llm_for_embed_text(self):
        """When a provider is wired, the LLM summary is used as the embedded text."""
        store = FakeVectorStore()
        provider = MagicMock()
        provider.answer.return_value = "Billing collator processes payment records in billing module."
        asm = VectorContextAssembler(store, provider=provider, confidence_min_score=0.0)
        asm.record(
            "implement billing collator",
            {"kind": "met", "files": ["billing/collate.py"]},
        )

        # The embedded text should be the LLM summary, not the raw task.
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert "Billing collator" in item.get("text", ""), (
            f"expected LLM summary as embedded text, got: {item.get('text')!r}"
        )

    def test_record_with_provider_failure_falls_back_to_structural(self):
        """When the LLM call fails, falls back to structural description without raising."""
        store = FakeVectorStore()
        provider = MagicMock()
        provider.answer.side_effect = RuntimeError("LLM unavailable")
        asm = VectorContextAssembler(store, provider=provider, confidence_min_score=0.0)
        # Must not raise.
        asm.record("fix the billing collator", {"kind": "met", "files": ["billing/collate.py"]})

        # Item should still have been upserted.
        coll = list(store._data.values())[0]
        assert len(coll) == 1, "expected exactly one item upserted despite LLM failure"

    def test_render_hits_shows_rich_payload_fields(self):
        """Context view must surface task, paths, symbols, and summary from payload."""
        store = FakeVectorStore()
        store.upsert([{
            "id": "outcome:12345",
            "text": "billing collator payment records",
            "payload": {
                "task": "implement billing collator",
                "paths": ["billing/collate.py"],
                "symbols": ["PaymentCollector"],
                "summary": "billing collator writes payment records",
                "kind": "met",
            },
        }])
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        ac = asm.assemble("billing collator payment")
        assert "billing/collate.py" in ac.context_view, (
            f"expected file path in context_view: {ac.context_view!r}"
        )
        assert "PaymentCollector" in ac.context_view, (
            f"expected symbol in context_view: {ac.context_view!r}"
        )
        assert "implement billing collator" in ac.context_view, (
            f"expected task text in context_view: {ac.context_view!r}"
        )


# ---------------------------------------------------------------------------
# HybridContextAssembler: record forwarding to vector arm -- Thing 2
# ---------------------------------------------------------------------------

class TestHybridRecordForwarding:
    """Thing 2: HybridContextAssembler.record forwards to the vector arm."""

    def test_hybrid_record_forwards_to_vector_assembler(self):
        """record() on the hybrid must forward to the vector assembler's record()."""
        records_from_vector: list = []

        class TrackingVectorAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()

            def record(self, task_text, outcome):
                records_from_vector.append((task_text, outcome))

        class NullKeywordAssembler:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()

            def record(self, task_text, outcome):
                pass

        hybrid = HybridContextAssembler(
            keyword=NullKeywordAssembler(),  # type: ignore[arg-type]
            vector=TrackingVectorAssembler(),  # type: ignore[arg-type]
        )
        hybrid.record("fix the billing pipeline", {"kind": "met", "files": ["billing/collate.py"]})

        assert len(records_from_vector) == 1, (
            f"expected 1 record forwarded to vector arm, got: {len(records_from_vector)}"
        )
        task, outcome = records_from_vector[0]
        assert task == "fix the billing pipeline"
        assert "billing/collate.py" in outcome.get("files", [])

    def test_hybrid_record_forwards_to_both_arms(self):
        """record() must forward to BOTH keyword and vector arms."""
        kw_records: list = []
        vec_records: list = []

        class TrackingKeyword:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                kw_records.append(task_text)

        class TrackingVector:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                vec_records.append(task_text)

        hybrid = HybridContextAssembler(
            keyword=TrackingKeyword(),  # type: ignore[arg-type]
            vector=TrackingVector(),  # type: ignore[arg-type]
        )
        hybrid.record("some task", {"kind": "met"})
        assert len(kw_records) == 1 and len(vec_records) == 1, (
            f"expected both arms to receive record(), kw={kw_records}, vec={vec_records}"
        )

    def test_hybrid_record_with_real_vector_assembler_upserts(self):
        """Integration: HybridContextAssembler.record reaches a real VectorContextAssembler."""
        from quest_ai_runner.adapters.file_context_store import FileContextStore

        store = FakeVectorStore()
        vec_asm = VectorContextAssembler(store, confidence_min_score=0.0)

        class NullKeyword:
            def assemble(self, task_text, *, meta=None):
                return AssembledContext()
            def record(self, task_text, outcome):
                pass

        hybrid = HybridContextAssembler(
            keyword=NullKeyword(),  # type: ignore[arg-type]
            vector=vec_asm,
        )
        hybrid.record("implement payment processor", {"kind": "met", "files": ["payment/processor.py"]})

        # The vector store should now have one item.
        hits = store.search("payment processor")
        assert len(hits) > 0, "expected record to reach vector store via hybrid"


# ---------------------------------------------------------------------------
# HybridContextAssembler
# ---------------------------------------------------------------------------

class TestHybridContextAssembler:
    def _make_stub_assembler(
        self,
        context_view: str = "",
        card_ids: Optional[List[str]] = None,
        stale: Optional[List[str]] = None,
    ) -> ContextAssembler:
        """Build a stub ContextAssembler that returns fixed output."""

        class StubAssembler:
            def __init__(self, view, ids, st):
                self._view = view
                self._ids = ids or []
                self._st = st or []
                self.records: list = []

            def assemble(self, task_text, *, meta=None):
                return AssembledContext(
                    context_view=self._view,
                    card_ids=list(self._ids),
                    stale=list(self._st),
                )

            def record(self, task_text, outcome):
                self.records.append((task_text, outcome))

        return StubAssembler(context_view, card_ids, stale)

    def test_fuses_both_non_empty_views(self):
        kw = self._make_stub_assembler("keyword content", ["kw-id"])
        vec = self._make_stub_assembler("vector content", ["vec-id"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("any task")
        assert "keyword content" in ac.context_view
        assert "vector content" in ac.context_view

    def test_labels_sections_clearly(self):
        kw = self._make_stub_assembler("kw-view", ["kw"])
        vec = self._make_stub_assembler("vec-view", ["vec"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert "Keyword context" in ac.context_view
        assert "Vector context" in ac.context_view

    def test_union_card_ids_deduped(self):
        kw = self._make_stub_assembler("kw-view", ["shared-id", "kw-only"])
        vec = self._make_stub_assembler("vec-view", ["shared-id", "vec-only"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert ac.card_ids.count("shared-id") == 1
        assert "kw-only" in ac.card_ids
        assert "vec-only" in ac.card_ids

    def test_union_stale_deduped(self):
        kw = self._make_stub_assembler("kw-view", stale=["file.py", "shared.py"])
        vec = self._make_stub_assembler("vec-view", stale=["shared.py", "other.py"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert ac.stale.count("shared.py") == 1
        assert "file.py" in ac.stale
        assert "other.py" in ac.stale

    def test_both_empty_returns_empty(self):
        kw = self._make_stub_assembler("")
        vec = self._make_stub_assembler("")
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert ac.context_view == ""
        assert ac.card_ids == []

    def test_only_keyword_empty_returns_vector(self):
        kw = self._make_stub_assembler("")
        vec = self._make_stub_assembler("vector content", ["vec-id"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert "vector content" in ac.context_view
        assert "vec-id" in ac.card_ids

    def test_only_vector_empty_returns_keyword(self):
        kw = self._make_stub_assembler("keyword content", ["kw-id"])
        vec = self._make_stub_assembler("")
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        assert "keyword content" in ac.context_view
        assert "kw-id" in ac.card_ids

    def test_record_forwards_to_both(self):
        kw = self._make_stub_assembler()
        vec = self._make_stub_assembler()
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        hybrid.record("task text", {"kind": "met"})
        assert len(kw.records) == 1  # type: ignore[attr-defined]
        assert len(vec.records) == 1  # type: ignore[attr-defined]

    def test_never_raises_on_broken_assembler(self):
        class Broken:
            def assemble(self, task_text, *, meta=None):
                raise RuntimeError("broken")
            def record(self, task_text, outcome):
                raise RuntimeError("broken")

        kw = Broken()
        vec = self._make_stub_assembler("vector content", ["vec-id"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)  # type: ignore[arg-type]
        ac = hybrid.assemble("task")
        assert isinstance(ac, AssembledContext)

    def test_with_real_file_context_store_and_fake_vector_store(self, tmp_path):
        """Integration test: HybridContextAssembler fusing a real FileContextStore
        (over a tiny temp repo) and a FakeVectorStore."""
        from quest_ai_runner.adapters.file_context_store import FileContextStore

        # Tiny repo.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "billing.py").write_text(
            "def generate_invoice(cid):\n    pass\n", encoding="utf-8"
        )
        cards_dir = tmp_path / "cards"
        kw_store = FileContextStore(
            str(cards_dir), repo_root=str(repo),
            auto_bootstrap=True, confidence_threshold=0.0,
        )

        vec_store = FakeVectorStore()
        vec_store.upsert([{"id": "billing-semantic", "text": "billing invoice generation"}])
        vec_asm = VectorContextAssembler(vec_store, confidence_min_score=0.0)

        hybrid = HybridContextAssembler(keyword=kw_store, vector=vec_asm)
        ac = hybrid.assemble("billing invoice generation")

        # Should include something from at least one assembler.
        assert ac.context_view != "" or ac.card_ids == []  # at minimum no crash

    def test_keyword_ids_come_before_vector_ids(self):
        kw = self._make_stub_assembler("kw-view", ["kw-first", "kw-second"])
        vec = self._make_stub_assembler("vec-view", ["vec-first"])
        hybrid = HybridContextAssembler(keyword=kw, vector=vec)
        ac = hybrid.assemble("task")
        kw_idx = [ac.card_ids.index(i) for i in ["kw-first", "kw-second"]]
        vec_idx = ac.card_ids.index("vec-first")
        assert all(k < vec_idx for k in kw_idx)


# ---------------------------------------------------------------------------
# New disciplined-memory tests: dedup/merge, recency decay, capacity bound
# ---------------------------------------------------------------------------

class TestRecordDedup:
    """Recording the same task twice must MERGE (one association, count==2)."""

    def test_same_task_twice_yields_one_association(self):
        store = FakeVectorStore()
        now_ts = 1_000_000.0
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("fix the billing pipeline", {"kind": "met", "ts": now_ts})
        asm.record("fix the billing pipeline", {"kind": "met", "ts": now_ts + 1})
        # Only one association should exist in the store.
        coll = list(store._data.values())[0]
        assert len(coll) == 1, (
            f"expected 1 association after two identical records, got {len(coll)}"
        )

    def test_same_task_twice_refreshes_ts(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("fix the billing pipeline", {"kind": "met", "ts": 1_000_000.0})
        asm.record("fix the billing pipeline", {"kind": "met", "ts": 2_000_000.0})
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert item["payload"]["ts"] == 2_000_000.0, (
            f"expected ts refreshed to 2_000_000, got {item['payload']['ts']}"
        )

    def test_different_tasks_yield_separate_associations(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("fix the billing pipeline", {"kind": "met", "ts": 1_000_000.0})
        asm.record("implement the payment collector", {"kind": "met", "ts": 1_000_001.0})
        coll = list(store._data.values())[0]
        assert len(coll) == 2, (
            f"expected 2 associations for two different tasks, got {len(coll)}"
        )

    def test_count_bumped_on_second_record(self):
        """The payload 'count' field must reflect re-records when caller passes _count."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        asm.record("fix the billing pipeline", {"kind": "met", "ts": 1_000_000.0, "_count": 1})
        asm.record("fix the billing pipeline", {"kind": "met", "ts": 1_000_001.0, "_count": 2})
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert item["payload"]["count"] == 2, (
            f"expected count==2 after two records, got {item['payload']['count']}"
        )


class TestRecencyDecay:
    """Recent associations must rank above old ones with equal raw scores."""

    def test_recent_ranks_above_old_with_equal_raw_score(self):
        """Two associations with equal raw scores: the recent one must rank first."""
        # We use an injectable clock so 'now' is deterministic.
        now = 1_000_000.0  # epoch seconds (arbitrary)
        old_ts = now - 400 * 86400   # 400 days ago
        recent_ts = now - 1 * 86400  # 1 day ago

        store = FakeVectorStore()
        # Both items have the same text so they get the same raw score.
        store.upsert([
            {
                "id": "old-assoc",
                "text": "billing payment pipeline",
                "payload": {"ts": old_ts, "task": "old task"},
            },
            {
                "id": "recent-assoc",
                "text": "billing payment pipeline",
                "payload": {"ts": recent_ts, "task": "recent task"},
            },
        ])

        asm = VectorContextAssembler(
            store,
            confidence_min_score=0.0,
            half_life_days=30.0,
            _clock=lambda: now,
        )
        ac = asm.assemble("billing payment")
        assert len(ac.card_ids) >= 2, "expected both hits to be returned"
        assert ac.card_ids[0] == "recent-assoc", (
            f"expected recent-assoc to rank first, got order: {ac.card_ids}"
        )

    def test_old_association_is_not_completely_dropped_when_score_above_gate(self):
        """Old associations should still appear (decayed score may still pass gate=0.0)."""
        now = 1_000_000.0
        old_ts = now - 400 * 86400

        store = FakeVectorStore()
        store.upsert([{
            "id": "old-assoc",
            "text": "billing payment pipeline",
            "payload": {"ts": old_ts},
        }])
        asm = VectorContextAssembler(
            store,
            confidence_min_score=0.0,
            half_life_days=30.0,
            _clock=lambda: now,
        )
        ac = asm.assemble("billing payment")
        assert "old-assoc" in ac.card_ids, "old association should still appear at gate=0.0"

    def test_age_appears_in_context_view(self):
        """The rendered context_view must mention age when ts is present."""
        now = 1_000_000.0
        ts_3_days_ago = now - 3 * 86400

        store = FakeVectorStore()
        store.upsert([{
            "id": "dated-assoc",
            "text": "billing payment",
            "payload": {"ts": ts_3_days_ago, "task": "billing"},
        }])
        asm = VectorContextAssembler(
            store,
            confidence_min_score=0.0,
            _clock=lambda: now,
        )
        ac = asm.assemble("billing payment")
        assert "days ago" in ac.context_view or "day ago" in ac.context_view or "today" in ac.context_view, (
            f"expected age label in context_view, got: {ac.context_view!r}"
        )

    def test_no_ts_hit_still_works_neutral_decay(self):
        """Hits with no ts must still appear (neutral 0.5 factor) and not raise."""
        store = FakeVectorStore()
        store.upsert([{
            "id": "no-ts",
            "text": "billing payment pipeline",
            "payload": {"task": "some task"},  # no ts key
        }])
        asm = VectorContextAssembler(store, confidence_min_score=0.0)
        ac = asm.assemble("billing payment")
        assert "no-ts" in ac.card_ids, "no-ts hit should still be returned with neutral decay"


class TestCapacityBound:
    """max_associations=3 + 5 distinct records -> count stays <=3 and oldest evicted."""

    def test_cap_enforced_after_five_records(self):
        store = FakeVectorStore()
        now = 1_000_000.0
        asm = VectorContextAssembler(
            store,
            confidence_min_score=0.0,
            max_associations=3,
            _clock=lambda: now,
        )
        for i in range(5):
            asm.record(
                f"distinct task number {i}",
                {"kind": "met", "ts": float(now + i)},
            )
        coll = list(store._data.values())[0]
        assert len(coll) <= 3, (
            f"expected at most 3 associations after 5 records (cap=3), got {len(coll)}"
        )

    def test_oldest_evicted_not_newest(self):
        """After cap enforcement, the oldest associations should be gone."""
        store = FakeVectorStore()
        base_ts = 1_000_000.0
        asm = VectorContextAssembler(
            store,
            confidence_min_score=0.0,
            max_associations=2,
        )
        # Record three tasks with explicit increasing timestamps.
        asm.record("oldest task alpha", {"kind": "met", "ts": base_ts})
        asm.record("middle task beta", {"kind": "met", "ts": base_ts + 1})
        asm.record("newest task gamma", {"kind": "met", "ts": base_ts + 2})

        coll = list(store._data.values())[0]
        remaining_ids = list(coll.keys())
        # The association for "oldest task alpha" should have been evicted.
        from quest_ai_runner.adapters.vector_context_assembler import _task_slug
        oldest_id = f"assoc:{_task_slug('oldest task alpha')}"
        assert oldest_id not in remaining_ids, (
            f"oldest association should have been evicted; remaining: {remaining_ids}"
        )

    def test_store_without_capacity_methods_does_not_raise(self):
        """A store lacking count/evict_oldest must not cause record() to raise."""
        class MinimalStore(VectorStoreBase):
            def search(self, q, *, scope=None, top_k=8):
                return []
            def upsert(self, items, *, scope=None):
                pass
            def sync(self, items, *, scope=None):
                return 0
            # No count or evict_oldest — must not raise

        asm = VectorContextAssembler(MinimalStore(), max_associations=1)
        asm.record("task one", {"kind": "met"})
        asm.record("task two", {"kind": "met"})  # would overflow cap but must not raise


class TestTimestampInPayload:
    """ts and count are written into the payload on every record."""

    def test_ts_in_payload_from_outcome(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store)
        asm.record("some task", {"kind": "met", "ts": 12345.0})
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert item["payload"].get("ts") == 12345.0, (
            f"expected ts==12345.0 in payload, got {item['payload']}"
        )

    def test_ts_in_payload_from_clock_when_not_provided(self):
        """When ts is not in outcome, the clock value is used."""
        fixed_ts = 99999.0
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, _clock=lambda: fixed_ts)
        asm.record("some task", {"kind": "met"})  # no ts in outcome
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert item["payload"].get("ts") == fixed_ts, (
            f"expected clock ts in payload, got {item['payload']}"
        )

    def test_count_defaults_to_1(self):
        store = FakeVectorStore()
        asm = VectorContextAssembler(store)
        asm.record("some task", {"kind": "met", "ts": 1.0})
        coll = list(store._data.values())[0]
        item = list(coll.values())[0]
        assert item["payload"].get("count") == 1, (
            f"expected count==1, got {item['payload']}"
        )


# ---------------------------------------------------------------------------
# QdrantVectorStore smoke test (skipped when qdrant-client is absent)
# ---------------------------------------------------------------------------

class TestQdrantVectorStoreSmoke:
    def test_constructor_requires_qdrant_client(self):
        """When qdrant-client is not installed, the constructor raises ImportError."""
        qdrant_client = pytest.importorskip("qdrant_client")
        # If we reach here, qdrant_client is installed; run a smoke test instead.
        from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

        def fake_embedder(texts: List[str]) -> List[List[float]]:
            return [[0.1] * 4 for _ in texts]

        with tempfile.TemporaryDirectory() as td:
            store = QdrantVectorStore(
                path=os.path.join(td, "qdrant"),
                embedder=fake_embedder,
                vector_size=4,
            )
            store.upsert([{"id": "item-1", "text": "hello world", "fingerprint": "fp1"}])
            hits = store.search("hello world")
            assert isinstance(hits, list)

    def test_qdrant_import_error_without_dep(self):
        """Without qdrant-client, importing QdrantVectorStore is fine; constructing fails."""
        try:
            import qdrant_client  # noqa: F401
            pytest.skip("qdrant-client is installed; skipping import-error test")
        except ImportError:
            pass
        from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore
        with pytest.raises(ImportError, match="quest-ai-runner\\[qdrant\\]"):
            QdrantVectorStore(path="/tmp/test")


# ---------------------------------------------------------------------------
# NEW: FileContextStore.export_for_embedding (cold-start bootstrap items)
# ---------------------------------------------------------------------------

class TestFileContextStoreExportForEmbedding:
    """export_for_embedding returns id/text/payload/fingerprint for each card."""

    def _make_tiny_repo(self, tmp_path) -> "FileContextStore":
        """Bootstrap a small FileContextStore over a temp repo with one Python file.

        Bootstrap is LLM-driven, so we wire a fake provider that returns a 'billing' topic
        card pinning billing.py with a billing/invoice summary."""
        from quest_ai_runner.adapters.file_context_store import FileContextStore
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "billing.py").write_text(
            '"""Billing module: generates invoices for customers."""\n\n'
            "def generate_invoice(cid):\n"
            '    """Generate an invoice for customer cid."""\n'
            "    pass\n",
            encoding="utf-8",
        )
        cards_dir = tmp_path / "cards"
        store = FileContextStore(
            str(cards_dir), repo_root=str(repo),
            auto_bootstrap=False, confidence_threshold=0.0,
        )
        provider = MagicMock()
        provider.list_models.return_value = []
        provider.answer.return_value = json.dumps([{
            "id": "billing",
            "name": "Billing",
            "keywords": ["billing", "invoice", "customer", "generate"],
            "summary": "Billing module: generates invoices for customers.",
            "files": ["billing.py"],
        }])
        store.bootstrap(root=str(repo), provider=provider)
        return store

    def test_returns_list_of_dicts(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        assert isinstance(items, list), "should return a list"
        assert len(items) > 0, "should return at least one item"

    def test_each_item_has_required_keys(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        for item in items:
            assert "id" in item, f"missing 'id': {item}"
            assert "text" in item, f"missing 'text': {item}"
            assert "payload" in item, f"missing 'payload': {item}"
            assert "fingerprint" in item, f"missing 'fingerprint': {item}"

    def test_item_id_prefixed_with_card(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        for item in items:
            assert item["id"].startswith("card:"), (
                f"expected id to start with 'card:', got {item['id']!r}"
            )

    def test_text_uses_docstring_description(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        # The billing.py module docstring text should appear as embed text.
        billing_item = next(
            (it for it in items if "billing" in it["id"].lower()), None
        )
        assert billing_item is not None, "expected an item for billing.py"
        assert "billing" in billing_item["text"].lower() or "invoice" in billing_item["text"].lower(), (
            f"expected docstring content in text, got: {billing_item['text']!r}"
        )

    def test_payload_has_paths_symbols_summary_kind(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        billing_item = next(
            (it for it in items if "billing" in it["id"].lower()), None
        )
        assert billing_item is not None, "expected a billing item"
        payload = billing_item["payload"]
        assert "paths" in payload, f"missing 'paths' in payload: {payload}"
        assert "symbols" in payload, f"missing 'symbols' in payload: {payload}"
        assert "summary" in payload, f"missing 'summary' in payload: {payload}"
        assert payload.get("kind") == "bootstrap", (
            f"expected kind='bootstrap', got {payload.get('kind')!r}"
        )

    def test_payload_paths_contains_source_file(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items = store.export_for_embedding()
        billing_item = next(
            (it for it in items if "billing" in it["id"].lower()), None
        )
        assert billing_item is not None
        paths = billing_item["payload"].get("paths", [])
        assert any("billing" in p for p in paths), (
            f"expected billing.py in paths, got: {paths}"
        )

    def test_fingerprint_is_stable_for_unchanged_files(self, tmp_path):
        store = self._make_tiny_repo(tmp_path)
        items1 = store.export_for_embedding()
        items2 = store.export_for_embedding()
        fp1 = {it["id"]: it["fingerprint"] for it in items1}
        fp2 = {it["id"]: it["fingerprint"] for it in items2}
        assert fp1 == fp2, "fingerprints should be stable for unchanged files"

    def test_empty_store_returns_empty_list(self, tmp_path):
        from quest_ai_runner.adapters.file_context_store import FileContextStore
        cards_dir = tmp_path / "empty_cards"
        store = FileContextStore(str(cards_dir), auto_bootstrap=False)
        items = store.export_for_embedding()
        assert items == [], "empty store should return []"

    def test_never_raises_on_corrupt_state(self, tmp_path):
        """export_for_embedding must return [] rather than raising on any error."""
        from quest_ai_runner.adapters.file_context_store import FileContextStore
        # Point at a non-existent path for cards_dir and no repo_root.
        store = FileContextStore(
            str(tmp_path / "nonexistent_cards"),
            auto_bootstrap=False,
        )
        result = store.export_for_embedding()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# NEW: VectorContextAssembler cold-start seeding via seed_source
# ---------------------------------------------------------------------------

class TestVectorContextAssemblerSeedSource:
    """seed_source syncs bootstrap items into the store on first assemble()."""

    def test_seed_source_called_on_first_assemble(self, tmp_path):
        """The seed_source callable must be called on the first assemble()."""
        call_log: List[int] = []

        def my_seed() -> List[Dict[str, Any]]:
            call_log.append(1)
            return [{"id": "card:boot1", "text": "bootstrap billing module", "payload": {"kind": "bootstrap"}, "fingerprint": "fp1"}]

        store = FakeVectorStore()
        asm = VectorContextAssembler(store, seed_source=my_seed, confidence_min_score=0.0)
        assert call_log == [], "seed_source should not be called at construction"
        asm.assemble("billing module")
        assert len(call_log) == 1, "seed_source should be called on first assemble()"

    def test_seed_source_not_called_twice(self, tmp_path):
        """The seed is guarded: must run at most once per instance."""
        call_log: List[int] = []

        def my_seed() -> List[Dict[str, Any]]:
            call_log.append(1)
            return [{"id": "card:boot1", "text": "some text", "payload": {}, "fingerprint": "fp1"}]

        store = FakeVectorStore()
        asm = VectorContextAssembler(store, seed_source=my_seed, confidence_min_score=0.0)
        asm.assemble("task one")
        asm.assemble("task two")
        assert len(call_log) == 1, (
            f"seed_source should only be called once, called {len(call_log)} times"
        )

    def test_seeded_item_is_searchable_after_first_assemble(self):
        """After seeding, the vector store contains items and a search returns hits."""
        store = FakeVectorStore()

        def my_seed() -> List[Dict[str, Any]]:
            return [{
                "id": "card:boot-billing",
                "text": "billing invoice generation module",
                "payload": {"paths": ["billing.py"], "kind": "bootstrap"},
                "fingerprint": "fp-billing",
            }]

        asm = VectorContextAssembler(store, seed_source=my_seed, confidence_min_score=0.0)
        ac = asm.assemble("billing invoice")
        assert "card:boot-billing" in ac.card_ids, (
            f"seeded item should appear in results, card_ids={ac.card_ids!r}"
        )

    def test_seed_fingerprint_based_no_reembed_on_second_instance(self):
        """sync() skips unchanged items: a second instance with same seed doesn't re-embed."""
        call_log: List[int] = []
        store = FakeVectorStore()

        items = [{"id": "card:boot1", "text": "billing module", "payload": {}, "fingerprint": "fp-stable"}]

        def my_seed() -> List[Dict[str, Any]]:
            call_log.append(1)
            return items

        # First instance: seeds.
        asm1 = VectorContextAssembler(store, seed_source=my_seed, confidence_min_score=0.0)
        asm1.assemble("billing module")
        assert len(call_log) == 1

        # Manually verify the item is in the store with its fingerprint.
        coll = store._data.get("_default", {})
        assert "card:boot1" in coll, "item should be in store after first seed"
        assert coll["card:boot1"]["fingerprint"] == "fp-stable"

        # Second instance with same items: sync should report 0 re-embeds.
        call_log2: List[int] = []

        def my_seed2() -> List[Dict[str, Any]]:
            call_log2.append(1)
            return items

        asm2 = VectorContextAssembler(store, seed_source=my_seed2, confidence_min_score=0.0)
        asm2.assemble("billing module")
        assert len(call_log2) == 1, "seed_source should still be called (to check)"
        # But the store's item should still have the same fingerprint (not re-inserted with a
        # different one), meaning sync() detected no change.
        coll2 = store._data.get("_default", {})
        assert coll2["card:boot1"]["fingerprint"] == "fp-stable"

    def test_seed_source_none_no_seeding(self):
        """Without seed_source, the store is NOT pre-populated."""
        store = FakeVectorStore()
        asm = VectorContextAssembler(store, seed_source=None, confidence_min_score=0.0)
        asm.assemble("billing invoice")
        # Store should be empty (no task associations written either since no record() called).
        total = sum(len(c) for c in store._data.values())
        assert total == 0, "store should remain empty without seed_source and without record()"

    def test_seed_source_error_does_not_raise(self):
        """A failing seed_source must be silently swallowed; assemble() must not raise."""
        def bad_seed() -> List[Dict[str, Any]]:
            raise RuntimeError("seed failed")

        store = FakeVectorStore()
        asm = VectorContextAssembler(store, seed_source=bad_seed, confidence_min_score=0.0)
        # Must not raise.
        ac = asm.assemble("any task")
        assert isinstance(ac, AssembledContext)

    def test_seed_with_real_file_context_store(self, tmp_path):
        """Integration: FileContextStore.export_for_embedding wired as seed_source."""
        from quest_ai_runner.adapters.file_context_store import FileContextStore

        # Build a tiny repo.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "payments.py").write_text(
            '"""Payment processing: handles payment capture and refunds."""\n\n'
            "def capture(amount):\n"
            '    """Capture a payment."""\n'
            "    pass\n",
            encoding="utf-8",
        )
        cards_dir = tmp_path / "cards"
        kw_store = FileContextStore(
            str(cards_dir), repo_root=str(repo),
            auto_bootstrap=False, confidence_threshold=0.0,
        )
        # Bootstrap the keyword store first so there are cards to export. Bootstrap is
        # LLM-driven, so wire a fake provider that returns a payments topic for payments.py.
        provider = MagicMock()
        provider.list_models.return_value = []
        provider.answer.return_value = json.dumps([{
            "id": "payments",
            "name": "Payments",
            "keywords": ["payment", "capture", "refund", "processing"],
            "summary": "Payment processing: handles payment capture and refunds.",
            "files": ["payments.py"],
        }])
        kw_store.bootstrap(root=str(repo), provider=provider)

        vec_store = FakeVectorStore()
        asm = VectorContextAssembler(
            vec_store,
            seed_source=kw_store.export_for_embedding,
            confidence_min_score=0.0,
        )
        # First assemble triggers seeding.
        ac = asm.assemble("payment capture processing")
        # The vector store should now have items.
        total_items = sum(len(c) for c in vec_store._data.values())
        assert total_items > 0, "seeding should populate the vector store"
        # And the assembly result should include a seeded card hit.
        assert any("card:" in cid for cid in ac.card_ids), (
            f"expected a seeded card in results, card_ids={ac.card_ids!r}"
        )


# ---------------------------------------------------------------------------
# NEW: resolve_context_assembler wires seed_source in hybrid mode
# ---------------------------------------------------------------------------

class TestResolveContextAssemblerSeedSourceWiring:
    """resolve_context_assembler passes seed_source=keyword.export_for_embedding
    when building the hybrid, so the vector arm is seeded on first use."""

    def test_hybrid_vector_arm_has_seed_source_wired(self, tmp_path):
        """When vector_store is set, the resolved vector arm has seed_source wired."""
        from quest_ai_runner.config import RunnerConfig, resolve_context_assembler, _AUTO_CONTEXT
        from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler

        # Build a minimal config with a vector store but no model_provider.
        vec_store = FakeVectorStore()

        # We need a tiny repo so FileContextStore can be built without crashing.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text(
            '"""Main entry point: orchestrates the application startup."""\n\n'
            "def run():\n"
            '    """Run the application."""\n'
            "    pass\n",
            encoding="utf-8",
        )

        # Bootstrap is LLM-driven, so wire a fake provider that returns a topic for main.py.
        # The provider feeds both the registry and the bootstrap topic identification.
        provider = MagicMock()
        provider.list_models.return_value = []
        provider.answer.return_value = json.dumps([{
            "id": "main",
            "name": "Main",
            "keywords": ["main", "entry", "startup", "run", "application"],
            "summary": "Main entry point: orchestrates the application startup.",
            "files": ["main.py"],
        }])

        cfg = RunnerConfig(
            quest_base_url="http://example.com",
            quest_api_key="qsk_test",
            retrieval=None,  # not needed for context wiring test
            model_provider=provider,
            vector_store=vec_store,
            corpus_root=str(repo),
            context_cards_dir=str(tmp_path / "cards"),
        )

        assembler = resolve_context_assembler(cfg)
        # resolve_context_assembler now returns a CompositeContextAssembler wrapping
        # [HybridContextAssembler, TurnContextStore]. Unwrap to find the hybrid arm.
        from quest_ai_runner.core.composite_assembler import CompositeContextAssembler
        assert isinstance(assembler, CompositeContextAssembler), (
            f"expected CompositeContextAssembler, got {type(assembler)!r}"
        )
        hybrid = next(
            (a for a in assembler._assemblers if isinstance(a, HybridContextAssembler)), None
        )
        assert hybrid is not None, "CompositeContextAssembler should contain a HybridContextAssembler"

        # Trigger first assemble — this should cause the vector arm to call sync().
        assembler.assemble("main module")

        # The vector store should have items from seeding.
        total_items = sum(len(c) for c in vec_store._data.values())
        assert total_items > 0, (
            "vector store should be populated after first assemble() via seed_source"
        )


# ---------------------------------------------------------------------------
# NEW: make_voyage_embedder factory + query_embedder split
# ---------------------------------------------------------------------------

class TestMakeVoyageEmbedder:
    """make_voyage_embedder returns a callable that calls the Voyage API."""

    def test_returns_callable(self):
        """make_voyage_embedder returns a callable when voyageai is importable."""
        import sys
        import types

        # Build a minimal voyageai stub so we never need the real package.
        va_stub = types.ModuleType("voyageai")

        class _FakeResult:
            embeddings = [[0.1, 0.2, 0.3]]

        class _FakeClient:
            def __init__(self, api_key=None):
                self.api_key = api_key

            def embed(self, texts, model, input_type):
                return _FakeResult()

        va_stub.Client = _FakeClient
        sys.modules["voyageai"] = va_stub
        try:
            from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder
            fn = make_voyage_embedder(model="voyage-3-lite", input_type="document")
            assert callable(fn)
        finally:
            del sys.modules["voyageai"]

    def test_callable_passes_correct_model_and_input_type(self):
        """The returned callable passes model and input_type to the Voyage client."""
        import sys
        import types

        calls: List[dict] = []

        va_stub = types.ModuleType("voyageai")

        class _FakeResult:
            embeddings = [[0.5, 0.6]]

        class _FakeClient:
            def __init__(self, api_key=None):
                pass

            def embed(self, texts, model, input_type):
                calls.append({"texts": texts, "model": model, "input_type": input_type})
                return _FakeResult()

        va_stub.Client = _FakeClient
        sys.modules["voyageai"] = va_stub
        try:
            from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder
            fn = make_voyage_embedder(model="voyage-3-lite", input_type="query")
            result = fn(["what is billing?"])
            assert len(calls) == 1
            assert calls[0]["model"] == "voyage-3-lite"
            assert calls[0]["input_type"] == "query"
            assert calls[0]["texts"] == ["what is billing?"]
            assert result == [[0.5, 0.6]]
        finally:
            del sys.modules["voyageai"]

    def test_callable_uses_env_model_when_no_model_arg(self):
        """When model is not given, VOYAGE_MODEL env var is used."""
        import sys
        import types

        calls: List[dict] = []

        va_stub = types.ModuleType("voyageai")

        class _FakeResult:
            embeddings = [[0.1]]

        class _FakeClient:
            def __init__(self, api_key=None):
                pass

            def embed(self, texts, model, input_type):
                calls.append({"model": model})
                return _FakeResult()

        va_stub.Client = _FakeClient
        sys.modules["voyageai"] = va_stub
        try:
            import os
            from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder
            os.environ["VOYAGE_MODEL"] = "voyage-env-model"
            try:
                fn = make_voyage_embedder(input_type="document")
                fn(["text"])
                assert calls[0]["model"] == "voyage-env-model"
            finally:
                del os.environ["VOYAGE_MODEL"]
        finally:
            del sys.modules["voyageai"]

    def test_callable_defaults_to_voyage_3_lite_when_no_env(self):
        """When model is not given and VOYAGE_MODEL is unset, voyage-3-lite is used."""
        import sys
        import types
        import os

        calls: List[dict] = []

        va_stub = types.ModuleType("voyageai")

        class _FakeResult:
            embeddings = [[0.1]]

        class _FakeClient:
            def __init__(self, api_key=None):
                pass

            def embed(self, texts, model, input_type):
                calls.append({"model": model})
                return _FakeResult()

        va_stub.Client = _FakeClient
        sys.modules["voyageai"] = va_stub
        # Ensure VOYAGE_MODEL is not set.
        os.environ.pop("VOYAGE_MODEL", None)
        try:
            from quest_ai_runner.adapters.qdrant_vector_store import make_voyage_embedder
            fn = make_voyage_embedder(input_type="document")
            fn(["text"])
            assert calls[0]["model"] == "voyage-3-lite"
        finally:
            del sys.modules["voyageai"]

    def test_missing_voyageai_raises_import_error_at_factory_time(self):
        """When voyageai is not installed, make_voyage_embedder raises ImportError immediately."""
        import sys
        import importlib

        # Setting sys.modules["voyageai"] = None makes Python raise ImportError
        # on any subsequent `import voyageai`, even if the package is installed on disk.
        original = sys.modules.get("voyageai", _SENTINEL := object())
        sys.modules["voyageai"] = None  # type: ignore[assignment]
        try:
            import quest_ai_runner.adapters.qdrant_vector_store as _mod
            importlib.reload(_mod)
            with pytest.raises(ImportError, match="pip install voyageai"):
                _mod.make_voyage_embedder(input_type="document")
        finally:
            if original is _SENTINEL:
                del sys.modules["voyageai"]
            else:
                sys.modules["voyageai"] = original  # type: ignore[assignment]
            # Reload the real module to restore the clean state.
            importlib.reload(_mod)


class TestQdrantVectorStoreQueryEmbedder:
    """query_embedder is used in search; embedder is used in upsert/sync."""

    def test_search_uses_query_embedder(self):
        """search() calls query_embedder, not embedder."""
        pytest.importorskip("qdrant_client")

        doc_calls: List[List[str]] = []
        query_calls: List[List[str]] = []

        def doc_embedder(texts: List[str]) -> List[List[float]]:
            doc_calls.append(texts)
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        def query_embedder(texts: List[str]) -> List[List[float]]:
            query_calls.append(texts)
            return [[0.9, 0.8, 0.7, 0.6] for _ in texts]

        from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

        with tempfile.TemporaryDirectory() as td:
            store = QdrantVectorStore(
                path=os.path.join(td, "qdrant"),
                embedder=doc_embedder,
                query_embedder=query_embedder,
                vector_size=4,
            )
            # search triggers query_embedder
            store.search("some query")
            assert len(query_calls) == 1, "query_embedder should be called once for search"
            assert len(doc_calls) == 0, "doc embedder should NOT be called during search"

    def test_upsert_uses_doc_embedder_not_query_embedder(self):
        """upsert() calls embedder, not query_embedder."""
        pytest.importorskip("qdrant_client")

        doc_calls: List[List[str]] = []
        query_calls: List[List[str]] = []

        def doc_embedder(texts: List[str]) -> List[List[float]]:
            doc_calls.append(texts)
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        def query_embedder(texts: List[str]) -> List[List[float]]:
            query_calls.append(texts)
            return [[0.9, 0.8, 0.7, 0.6] for _ in texts]

        from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

        with tempfile.TemporaryDirectory() as td:
            store = QdrantVectorStore(
                path=os.path.join(td, "qdrant"),
                embedder=doc_embedder,
                query_embedder=query_embedder,
                vector_size=4,
            )
            store.upsert([{"id": "item-1", "text": "hello world"}])
            assert len(doc_calls) == 1, "doc embedder should be called once for upsert"
            assert len(query_calls) == 0, "query_embedder should NOT be called during upsert"

    def test_single_embedder_caller_backward_compatible(self):
        """When query_embedder is omitted, search uses the same embedder (backward compat)."""
        pytest.importorskip("qdrant_client")

        all_calls: List[List[str]] = []

        def single_embedder(texts: List[str]) -> List[List[float]]:
            all_calls.append(texts)
            return [[0.5, 0.5, 0.5, 0.5] for _ in texts]

        from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

        with tempfile.TemporaryDirectory() as td:
            store = QdrantVectorStore(
                path=os.path.join(td, "qdrant"),
                embedder=single_embedder,
                vector_size=4,
            )
            store.upsert([{"id": "item-1", "text": "hello world"}])
            store.search("hello")
            # Both upsert and search should have used the single_embedder.
            assert len(all_calls) == 2, (
                f"expected 2 calls (upsert + search), got {len(all_calls)}"
            )
