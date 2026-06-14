"""Offline tests for BM25ContentStore.

All tests are fully offline: no network, no API key, no LLM calls.
Tests that exercise the real BM25 index are guarded by
``pytest.importorskip("bm25s")`` so they are cleanly skipped when
the ``[bm25]`` optional extra is not installed.

Demonstrates that BM25ContentStore finds files by their ACTUAL CONTENT,
not just by card summaries or keyword metadata (the content-search advantage
over the existing IDF arm).
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from quest_ai_runner.core.adapters import AssembledContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_corpus(tmp_path: Path) -> Path:
    """Create a tiny repo with a few .py files containing DISTINCTIVE identifiers.

    * alpha.py  -- contains the identifier  XFCALLBACK_7Q2
    * beta.py   -- contains the identifier  ZYGOTE_MERGE_PIPELINE
    * gamma.py  -- contains both            ORDINARY_FUNCTION and helper text
    * delta.py  -- contains only plain words that won't distinguish anything
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "alpha.py").write_text(
        textwrap.dedent("""\
            # Alpha module
            def XFCALLBACK_7Q2(context):
                \"\"\"Handle the XF callback event 7Q2.\"\"\"
                return context.dispatch()
        """),
        encoding="utf-8",
    )

    (repo / "beta.py").write_text(
        textwrap.dedent("""\
            # Beta module
            class ZYGOTE_MERGE_PIPELINE:
                \"\"\"Merge zygote payloads through the pipeline.\"\"\"
                def run(self):
                    pass
        """),
        encoding="utf-8",
    )

    (repo / "gamma.py").write_text(
        textwrap.dedent("""\
            # Gamma module
            def ORDINARY_FUNCTION():
                \"\"\"A garden-variety helper used by other modules.\"\"\"
                pass

            def helper():
                pass
        """),
        encoding="utf-8",
    )

    (repo / "delta.py").write_text(
        textwrap.dedent("""\
            # Delta module
            # This file has only generic words: setup, configure, initialize.
            def setup():
                pass
        """),
        encoding="utf-8",
    )

    return repo


def _make_store(repo: Path, **kwargs):
    """Construct a BM25ContentStore (skips if bm25s absent)."""
    bm25s = pytest.importorskip("bm25s")  # noqa: F841 -- ensures bm25s is available
    from quest_ai_runner.adapters.bm25_content_store import BM25ContentStore
    return BM25ContentStore(root=str(repo), **kwargs)


# ---------------------------------------------------------------------------
# Import-guard test (always runs, no bm25s needed)
# ---------------------------------------------------------------------------

class TestImportGuard:
    def test_adapters_import_without_bm25s(self):
        """``import quest_ai_runner.adapters`` must not raise even if bm25s is absent."""
        import quest_ai_runner.adapters  # noqa: F401  -- must not raise

    def test_constructor_raises_import_error_without_bm25s(self, tmp_path, monkeypatch):
        """Constructor raises ImportError with hint when bm25s is not importable."""
        import sys
        # Simulate bm25s being absent by temporarily hiding it.
        original = sys.modules.pop("bm25s", None)
        # Also force the bm25_content_store module to re-import bm25s.
        bm25_mod = sys.modules.pop(
            "quest_ai_runner.adapters.bm25_content_store", None
        )
        try:
            from quest_ai_runner.adapters.bm25_content_store import BM25ContentStore
            with pytest.raises(ImportError, match="quest-ai-runner\\[bm25\\]"):
                BM25ContentStore(root=str(tmp_path))
        finally:
            # Restore originals so other tests are unaffected.
            if original is not None:
                sys.modules["bm25s"] = original
            if bm25_mod is not None:
                sys.modules["quest_ai_runner.adapters.bm25_content_store"] = bm25_mod


# ---------------------------------------------------------------------------
# Content-search advantage: BM25 finds files by their content, not summaries
# ---------------------------------------------------------------------------

class TestContentSearchAdvantage:
    def test_finds_distinctive_identifier_in_content(self, tmp_path):
        """BM25 over content finds alpha.py for 'XFCALLBACK_7Q2', which is ONLY
        in the file content -- not in any card summary or keyword metadata."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("XFCALLBACK_7Q2")

        assert isinstance(ac, AssembledContext)
        assert len(ac.card_ids) > 0, "Expected at least one hit"
        # alpha.py must be the TOP hit -- it is the only file containing XFCALLBACK_7Q2.
        assert ac.card_ids[0] == "alpha.py", (
            f"Expected 'alpha.py' as top hit, got {ac.card_ids}"
        )

    def test_finds_distinctive_class_name(self, tmp_path):
        """BM25 finds beta.py for 'ZYGOTE_MERGE_PIPELINE'."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("ZYGOTE_MERGE_PIPELINE")

        assert len(ac.card_ids) > 0
        assert ac.card_ids[0] == "beta.py", (
            f"Expected 'beta.py' as top hit, got {ac.card_ids}"
        )

    def test_context_view_contains_snippet(self, tmp_path):
        """The rendered context_view includes a snippet from the matching file."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("XFCALLBACK_7Q2")

        # The context_view should mention the file path.
        assert "alpha.py" in ac.context_view

    def test_no_match_returns_empty_context(self, tmp_path):
        """A query that matches nothing returns an empty AssembledContext."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("ZZZZNONEXISTENT_TOKEN_QWERTY_99999")

        assert ac.context_view == ""
        assert ac.card_ids == []


# ---------------------------------------------------------------------------
# Confidence gate
# ---------------------------------------------------------------------------

class TestConfidenceGate:
    def test_high_threshold_gates_out_weak_hits(self, tmp_path):
        """With a very high threshold, no hits qualify and empty context is returned."""
        repo = _make_temp_corpus(tmp_path)
        # Use a threshold that no realistic BM25 score will clear.
        store = _make_store(repo, confidence_threshold=1_000_000.0)

        ac = store.assemble("XFCALLBACK_7Q2")

        assert ac.context_view == ""
        assert ac.card_ids == []

    def test_zero_threshold_keeps_all_positive_hits(self, tmp_path):
        """With threshold=0.0 all positive-scoring hits are kept."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("ORDINARY_FUNCTION helper")

        # gamma.py contains both tokens; it should be a hit.
        assert "gamma.py" in ac.card_ids


# ---------------------------------------------------------------------------
# Parallel multi-query with a stub provider
# ---------------------------------------------------------------------------

class TestParallelMultiQuery:
    def _make_stub_provider(self, extra_queries: str) -> MagicMock:
        """Provider whose ``answer()`` returns ``extra_queries`` (newline-separated)."""
        provider = MagicMock()
        provider.answer.return_value = extra_queries
        return provider

    def test_parallel_multi_query_fuses_hits(self, tmp_path):
        """With a stub provider, extra queries are generated and searched in parallel;
        hits from all queries are fused by keeping the best score per file."""
        repo = _make_temp_corpus(tmp_path)

        # Provider returns two extra queries: one targeting alpha, one targeting beta.
        provider = self._make_stub_provider(
            "XFCALLBACK_7Q2 dispatch\nZYGOTE_MERGE_PIPELINE run"
        )
        store = _make_store(
            repo,
            provider=provider,
            num_queries=2,
            confidence_threshold=0.0,
        )

        ac = store.assemble("module callback pipeline")

        # Both alpha.py and beta.py should be in the results.
        assert "alpha.py" in ac.card_ids
        assert "beta.py" in ac.card_ids

    def test_provider_answer_called_once(self, tmp_path):
        """The LLM is called exactly once (for query generation)."""
        repo = _make_temp_corpus(tmp_path)
        provider = self._make_stub_provider("XFCALLBACK_7Q2")
        store = _make_store(
            repo,
            provider=provider,
            num_queries=1,
            confidence_threshold=0.0,
        )

        store.assemble("some task text")

        assert provider.answer.call_count == 1

    def test_no_provider_uses_raw_query_only(self, tmp_path):
        """Without a provider, only the raw task text is used as the query."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, provider=None, confidence_threshold=0.0)

        # Should still find the file by its content.
        ac = store.assemble("ZYGOTE_MERGE_PIPELINE")
        assert "beta.py" in ac.card_ids

    def test_provider_failure_falls_back_gracefully(self, tmp_path):
        """If the provider raises, extra queries are skipped and raw query is used."""
        repo = _make_temp_corpus(tmp_path)
        provider = MagicMock()
        provider.answer.side_effect = RuntimeError("provider offline")

        store = _make_store(
            repo,
            provider=provider,
            num_queries=3,
            confidence_threshold=0.0,
        )

        # Should still return results using only the raw query.
        ac = store.assemble("XFCALLBACK_7Q2")
        assert isinstance(ac, AssembledContext)


# ---------------------------------------------------------------------------
# AUTO-UPDATE: changing file content re-indexes
# ---------------------------------------------------------------------------

class TestAutoUpdate:
    def test_changed_content_becomes_findable(self, tmp_path):
        """After updating a file's content, the new term becomes findable and
        the store picks up the change automatically (sha256 fingerprint check)."""
        repo = tmp_path / "repo"
        repo.mkdir()

        target = repo / "target.py"
        target.write_text("def initial_setup():\n    pass\n", encoding="utf-8")

        store = _make_store(repo, confidence_threshold=0.0)

        # Before update: SENTINEL_TERM_XYZ99 should NOT be found.
        ac_before = store.assemble("SENTINEL_TERM_XYZ99")
        assert "target.py" not in ac_before.card_ids

        # Update the file to contain SENTINEL_TERM_XYZ99.
        target.write_text(
            "def SENTINEL_TERM_XYZ99():\n    return True\n",
            encoding="utf-8",
        )

        # After update: should be found.
        ac_after = store.assemble("SENTINEL_TERM_XYZ99")
        assert "target.py" in ac_after.card_ids

    def test_old_term_no_longer_top_after_content_change(self, tmp_path):
        """After rewriting a file to remove its distinctive term, it is no longer
        the top hit for a query on that term."""
        repo = tmp_path / "repo"
        repo.mkdir()

        (repo / "unique.py").write_text(
            "DISTINCTIVE_OLD_TERM = 1\n", encoding="utf-8"
        )

        store = _make_store(repo, confidence_threshold=0.0)

        # Confirm the old term is found.
        ac1 = store.assemble("DISTINCTIVE_OLD_TERM")
        assert "unique.py" in ac1.card_ids

        # Rewrite the file to remove the old term.
        (repo / "unique.py").write_text(
            "REPLACEMENT_CONTENT = 2\n", encoding="utf-8"
        )

        # After auto-update, the old term should no longer find unique.py.
        ac2 = store.assemble("DISTINCTIVE_OLD_TERM")
        # unique.py either no longer appears, or a different file is top.
        if ac2.card_ids:
            assert ac2.card_ids[0] != "unique.py"

    def test_new_file_is_indexed_on_next_assemble(self, tmp_path):
        """A file added AFTER the first assemble is picked up on the next call."""
        repo = tmp_path / "repo"
        repo.mkdir()

        (repo / "existing.py").write_text("x = 1\n", encoding="utf-8")
        store = _make_store(repo, confidence_threshold=0.0)

        # Force initial index build.
        store.assemble("existing module")

        # Add a new file.
        (repo / "newcomer.py").write_text(
            "LATE_ARRIVAL_IDENTIFIER = True\n", encoding="utf-8"
        )

        # Should find the new file on the next call.
        ac = store.assemble("LATE_ARRIVAL_IDENTIFIER")
        assert "newcomer.py" in ac.card_ids


# ---------------------------------------------------------------------------
# Never-raise contract
# ---------------------------------------------------------------------------

class TestNeverRaise:
    def test_assemble_never_raises_on_empty_corpus(self, tmp_path):
        """An empty root directory must not raise; it returns an empty context."""
        repo = tmp_path / "empty_repo"
        repo.mkdir()
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("any query")
        assert isinstance(ac, AssembledContext)

    def test_assemble_never_raises_on_nonexistent_root(self, tmp_path):
        """A non-existent root must not raise."""
        bm25s = pytest.importorskip("bm25s")  # noqa: F841
        from quest_ai_runner.adapters.bm25_content_store import BM25ContentStore

        store = BM25ContentStore(root=str(tmp_path / "does_not_exist"))
        ac = store.assemble("anything")
        assert isinstance(ac, AssembledContext)

    def test_record_never_raises(self, tmp_path):
        """``record()`` is a no-op and must never raise."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo)

        store.record("some task", {"kind": "met"})  # must not raise

    def test_assemble_returns_assembled_context_type(self, tmp_path):
        """``assemble()`` always returns an AssembledContext instance."""
        repo = _make_temp_corpus(tmp_path)
        store = _make_store(repo, confidence_threshold=0.0)

        ac = store.assemble("XFCALLBACK_7Q2")
        assert isinstance(ac, AssembledContext)
        assert isinstance(ac.card_ids, list)
        assert isinstance(ac.stale, list)


# ---------------------------------------------------------------------------
# max_in_view cap
# ---------------------------------------------------------------------------

class TestMaxInView:
    def test_max_in_view_limits_results(self, tmp_path):
        """The number of results in card_ids is capped at max_in_view."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Write 10 files each containing "common_token".
        for i in range(10):
            (repo / f"file_{i:02d}.py").write_text(
                f"common_token = {i}\n", encoding="utf-8"
            )

        store = _make_store(repo, max_in_view=3, confidence_threshold=0.0)
        ac = store.assemble("common_token")

        assert len(ac.card_ids) <= 3
