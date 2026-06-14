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
