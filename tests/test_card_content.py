"""Offline tests for the source-agnostic card CONTENT model on FileContextStore.

A context card is no longer file-only: it can carry an optional ``content`` list of TYPED items,
each either a REFERENCE (resolved fresh on use) or an LLM NOTE. Files become one reference type, so
a card may have zero files. References resolve through a wired ``{type: ReferenceResolver}`` registry
(``file``/``note`` built in; collection/conversation/query consumer-injected). Resolution is
recency-bounded. These tests run OFFLINE with stub resolvers — no network, no model provider.

All assertions here are additive: the existing file-only behaviour (see test_context_assembler.py)
must stay byte-for-byte unchanged, which those tests continue to prove.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from quest_ai_runner.adapters.file_context_store import (
    FileContextStore,
    _normalize_content,
    _rank_content_by_recency_relevance,
    _trim_content_by_recency,
)
from quest_ai_runner.adapters.card_content_render import (
    content_identity_key,
    dedupe_content,
)
from quest_ai_runner.adapters.reference_resolver import (
    NoteResolver,
    ReferenceResolver,
    build_resolver_registry,
    _render_unresolved,
)
from quest_ai_runner.core.adapters import AssembledContext


# ---------------------------------------------------------------------------
# Helpers / stub resolvers
# ---------------------------------------------------------------------------

class _StubCollectionResolver:
    """A stub ``collection`` resolver that returns FRESH text injected at assemble time.

    Tracks calls so a test can assert the reference was resolved live (not a stale snapshot).
    """

    def __init__(self, text: str = "FRESH_COLLECTION_ROWS") -> None:
        self.text = text
        self.calls: list = []

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        self.calls.append(locator)
        return f"{self.text} for {locator.get('name', '?')}"


def _content_card(card_id: str, *, keywords, summary="", content, files=None) -> Dict[str, Any]:
    return {
        "id": card_id,
        "keywords": keywords,
        "summary": summary,
        "files": files or [],
        "content": content,
        "conventions": [],
        "provenance": {"created_by_task": "", "model": "", "created_at": "",
                       "last_verified_at": ""},
        "usage_count": 0,
        "last_outcome": "unknown",
    }


def _write(cards_dir: Path, card: Dict[str, Any]) -> None:
    (cards_dir / f"{card['id']}.json").write_text(json.dumps(card, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# ReferenceResolver registry + built-ins
# ---------------------------------------------------------------------------

class TestResolverRegistry:
    def test_note_resolver_returns_text(self):
        assert NoteResolver().resolve({"text": "hello"}) == "hello"

    def test_note_resolver_truncates(self):
        out = NoteResolver().resolve({"text": "x" * 100}, max_chars=10)
        assert len(out) <= 10
        assert out.endswith("…")

    def test_note_resolver_never_raises_on_bad_locator(self):
        assert NoteResolver().resolve(None) == ""  # type: ignore[arg-type]
        assert NoteResolver().resolve({}) == ""

    def test_build_registry_has_note_builtin(self):
        reg = build_resolver_registry()
        assert "note" in reg
        assert isinstance(reg["note"], ReferenceResolver)

    def test_build_registry_wires_file_when_reader_given(self):
        reg = build_resolver_registry(file_read_text=lambda p, n: "content")
        assert "file" in reg
        assert reg["file"].resolve({"path": "x"}) == "content"

    def test_consumer_resolver_wins_on_collision(self):
        custom = _StubCollectionResolver()
        reg = build_resolver_registry(consumer_resolvers={"note": custom})
        assert reg["note"] is custom

    def test_unresolved_pointer_lines(self):
        assert "unresolved" in _render_unresolved("collection", {"name": "x", "id": "y"})
        assert "unresolved" in _render_unresolved("conversation", {"conv_id": "c1"})
        assert "unresolved" in _render_unresolved("query", {"query": "find x"})

    def test_unresolved_never_raises(self):
        assert _render_unresolved("collection", None) != ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Content normalization / recency ranking / trim
# ---------------------------------------------------------------------------

class TestContentHelpers:
    def test_normalize_drops_non_dicts_and_defaults(self):
        out = _normalize_content([{"type": "note", "locator": {"text": "a"}}, "junk", 5])
        assert len(out) == 1
        assert out[0]["type"] == "note"
        assert out[0]["ts"] == 0.0
        assert out[0]["id"]  # synthesized

    def test_normalize_missing_content_is_empty(self):
        assert _normalize_content(None) == []
        assert _normalize_content("not a list") == []

    def test_rank_prefers_relevance_then_recency(self):
        items = [
            {"id": "old_rel", "type": "note", "locator": {"text": "alpha topic"}, "ts": 1.0,
             "why": "alpha"},
            {"id": "new_irrel", "type": "note", "locator": {"text": "zeta unrelated"}, "ts": 100.0,
             "why": "zeta"},
        ]
        ranked = _rank_content_by_recency_relevance(items, {"alpha"}, limit=2)
        assert ranked[0]["id"] == "old_rel"  # relevance beats raw recency

    def test_rank_limit_caps(self):
        items = [{"id": f"n{i}", "type": "note", "locator": {"text": "x"}, "ts": float(i),
                  "why": ""} for i in range(10)]
        assert len(_rank_content_by_recency_relevance(items, set(), limit=3)) == 3

    def test_trim_keeps_newest_in_original_order(self):
        items = [{"id": f"n{i}", "type": "note", "locator": {}, "ts": float(i), "why": ""}
                 for i in range(5)]
        kept = _trim_content_by_recency(items, max_items=3)
        assert [it["id"] for it in kept] == ["n2", "n3", "n4"]  # newest 3, original order


# ---------------------------------------------------------------------------
# assemble(): collection ref resolves fresh, recency bound, note renders, no-file card selectable
# ---------------------------------------------------------------------------

class TestAssembleContent:
    def test_collection_ref_resolves_fresh_via_stub(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write(cards_dir, _content_card(
            "coll-card", keywords=["payments", "refund"],
            content=[{"id": "c1", "type": "collection",
                      "locator": {"name": "payments", "id": "p1", "query": "refund"},
                      "ts": 10.0, "why": "live payments"}],
        ))
        stub = _StubCollectionResolver("LIVE_ROWS")
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0,
                                 reference_resolvers={"collection": stub})
        ac = store.assemble("payments refund")
        assert "coll-card" in ac.card_ids
        assert "LIVE_ROWS for payments" in ac.context_view
        assert stub.calls, "the collection ref must be resolved live during assemble()"

    def test_recency_bound_resolves_only_top_n(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        items = [{"id": f"n{i}", "type": "note",
                  "locator": {"text": f"item{i} alpha detail"}, "ts": float(i), "why": "alpha"}
                 for i in range(10)]
        _write(cards_dir, _content_card("big", keywords=["alpha"], content=items))
        # max_card_refs=3: only the 3 most-recent (item7/8/9) resolve.
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0, max_card_refs=3)
        ac = store.assemble("alpha")
        assert "item9" in ac.context_view
        assert "item8" in ac.context_view
        assert "item0" not in ac.context_view  # oldest skipped
        assert "item1" not in ac.context_view

    def test_note_only_card_selected_rendered_and_embedded(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write(cards_dir, _content_card(
            "note-only", keywords=["onboarding", "policy"], summary="",
            content=[{"id": "n1", "type": "note",
                      "locator": {"text": "never skip the onboarding policy step"},
                      "ts": 5.0, "why": "policy"}],
            files=[],  # ZERO files
        ))
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0)
        ac = store.assemble("onboarding policy question")
        # selected + rendered
        assert "note-only" in ac.card_ids
        assert "never skip the onboarding policy step" in ac.context_view
        # embeddable: note text appears in export_for_embedding
        items = store.export_for_embedding()
        texts = " ".join(it["text"] for it in items)
        assert "onboarding policy step" in texts

    def test_unwired_reference_type_renders_graceful_line(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write(cards_dir, _content_card(
            "needs-coll", keywords=["insights"],
            content=[{"id": "c1", "type": "collection",
                      "locator": {"name": "insights", "id": "abc"}, "ts": 1.0, "why": "data"}],
        ))
        # No collection resolver wired.
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0)
        ac = store.assemble("insights")  # must not raise
        assert isinstance(ac, AssembledContext)
        assert "needs-coll" in ac.card_ids
        assert "unresolved" in ac.context_view

    def test_assemble_never_raises_on_broken_resolver(self, tmp_path):
        class _Boom:
            def resolve(self, locator, *, max_chars=2000):
                raise RuntimeError("boom")

        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        _write(cards_dir, _content_card(
            "x", keywords=["alpha"],
            content=[{"id": "c1", "type": "collection", "locator": {"name": "n"}, "ts": 1.0,
                      "why": "alpha"}],
        ))
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0,
                                 reference_resolvers={"collection": _Boom()})
        ac = store.assemble("alpha")  # resolver raises internally; assemble must not
        assert isinstance(ac, AssembledContext)
        assert "x" in ac.card_ids
        # A raising resolver yields "" -> graceful unresolved line, never a crash.
        assert "unresolved" in ac.context_view


# ---------------------------------------------------------------------------
# Card-update API: add / update (correct) / remove round-trip + recency trim
# ---------------------------------------------------------------------------

class TestCardUpdateAPI:
    def test_add_content_persists_and_round_trips(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        ok = store.add_content("card1", {"id": "n1", "type": "note",
                                         "locator": {"text": "first note"}, "ts": 1.0,
                                         "why": "w"})
        assert ok
        card = json.loads((cards_dir / "card1.json").read_text())
        assert card["content"][0]["id"] == "n1"
        assert card["content"][0]["locator"]["text"] == "first note"

    def test_update_content_corrects_in_place(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.add_content("card1", {"id": "n1", "type": "note",
                                    "locator": {"text": "wrong"}, "ts": 1.0, "why": ""})
        ok = store.update_content("card1", "n1",
                                  {"type": "note", "locator": {"text": "corrected"}, "ts": 2.0,
                                   "why": "fixed"})
        assert ok
        card = json.loads((cards_dir / "card1.json").read_text())
        n1 = [c for c in card["content"] if c["id"] == "n1"][0]
        assert n1["locator"]["text"] == "corrected"
        assert n1["why"] == "fixed"

    def test_update_content_appends_when_id_unknown(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.add_content("card1", {"id": "n1", "type": "note", "locator": {"text": "a"},
                                    "ts": 1.0, "why": ""})
        store.update_content("card1", "missing",
                             {"id": "n2", "type": "note", "locator": {"text": "b"}, "ts": 2.0,
                              "why": ""})
        card = json.loads((cards_dir / "card1.json").read_text())
        ids = {c["id"] for c in card["content"]}
        assert ids == {"n1", "n2"}

    def test_remove_content_persists(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.add_content("card1", {"id": "n1", "type": "note", "locator": {"text": "a"},
                                    "ts": 1.0, "why": ""})
        store.add_content("card1", {"id": "n2", "type": "note", "locator": {"text": "b"},
                                    "ts": 2.0, "why": ""})
        assert store.remove_content("card1", "n1")
        card = json.loads((cards_dir / "card1.json").read_text())
        assert {c["id"] for c in card["content"]} == {"n2"}

    def test_update_card_batch(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.add_content("card1", {"id": "n1", "type": "note", "locator": {"text": "a"},
                                    "ts": 1.0, "why": ""})
        ok = store.update_card(
            "card1",
            add=[{"id": "n3", "type": "note", "locator": {"text": "c"}, "ts": 3.0, "why": ""}],
            replace=[("n1", {"id": "n1", "type": "note", "locator": {"text": "A2"}, "ts": 1.5,
                             "why": ""})],
        )
        assert ok
        card = json.loads((cards_dir / "card1.json").read_text())
        by_id = {c["id"]: c for c in card["content"]}
        assert by_id["n1"]["locator"]["text"] == "A2"
        assert "n3" in by_id

    def test_recency_trim_caps_stored_set(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        # Add far more than the cap, with increasing ts.
        for i in range(250):
            store.add_content("big", {"id": f"n{i}", "type": "note",
                                      "locator": {"text": f"item {i}"}, "ts": float(i), "why": ""})
        card = json.loads((cards_dir / "big.json").read_text())
        assert len(card["content"]) <= 200  # _MAX_CARD_CONTENT_ITEMS
        # The newest item must survive; the oldest must be trimmed.
        ids = {c["id"] for c in card["content"]}
        assert "n249" in ids
        assert "n0" not in ids

    def test_update_api_never_raises_on_unwritable_dir(self, tmp_path):
        bad_dir = tmp_path / "ro"
        bad_dir.mkdir()
        bad_dir.chmod(0o444)
        try:
            store = FileContextStore(str(bad_dir / "cards"))
            # Must not raise; returns False on a failed write.
            assert store.add_content("x", {"type": "note", "locator": {"text": "t"}}) in (True, False)
        finally:
            bad_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# record() generalization: append non-file content via outcome["content"]
# ---------------------------------------------------------------------------

class TestRecordContent:
    def test_record_appends_content_items(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.record("track the refund policy", {
            "kind": "answer",
            "content": [{"id": "n1", "type": "note",
                         "locator": {"text": "refunds over 100 need confirm"}, "ts": 1.0,
                         "why": "policy"}],
        })
        (card_file,) = list(cards_dir.glob("*.json"))
        card = json.loads(card_file.read_text())
        assert any(c["id"] == "n1" for c in card.get("content", []))
        # File-pinning behaviour is untouched: no files were given, so files stays empty.
        assert card.get("files", []) == []

    def test_record_accepts_single_content_dict(self, tmp_path):
        cards_dir = tmp_path / "cards"
        store = FileContextStore(str(cards_dir))
        store.record("note a thing", {
            "kind": "answer",
            "content": {"type": "note", "locator": {"text": "single"}, "ts": 1.0, "why": ""},
        })
        (card_file,) = list(cards_dir.glob("*.json"))
        card = json.loads(card_file.read_text())
        assert len(card.get("content", [])) == 1


# ---------------------------------------------------------------------------
# BACKWARD-COMPAT: a file-only card with NO content and NO resolvers is unchanged
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_file_only_card_view_unchanged_shape(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        real = tmp_path / "mod.py"
        real.write_text("# hi\n", encoding="utf-8")
        card = {
            "id": "filecard", "keywords": ["module", "python", "implementation"],
            "summary": "the module", "files": [
                {"path": str(real), "sha256": "", "mtime": 0.0, "git_sha": "",
                 "why": "entry", "symbols": ["run"]}],
            "conventions": [], "provenance": {}, "usage_count": 0, "last_outcome": "unknown",
        }
        (cards_dir / "filecard.json").write_text(json.dumps(card), encoding="utf-8")
        store = FileContextStore(str(cards_dir))
        ac = store.assemble("module python implementation")
        # The classic file block is still present; no Content section appears.
        assert "Files:" in ac.context_view
        assert "mod.py" in ac.context_view
        assert "Content:" not in ac.context_view


# ---------------------------------------------------------------------------
# Reference DEDUP on write: re-adding the same collection id / file path / note
# merges into the existing item (newest ts + freshest why) instead of bloating.
# ---------------------------------------------------------------------------

class TestContentDedup:
    def test_identity_key_by_locator(self):
        # collection -> id; file -> path; note -> text; case/space-insensitive.
        assert content_identity_key(
            {"type": "collection", "locator": {"id": "COL-1", "name": "Pricing"}}
        ) == content_identity_key(
            {"type": "collection", "locator": {"id": "col-1", "name": "Other"}}
        )
        assert content_identity_key({"type": "file", "locator": {"path": "a/b.py"}}) \
            == content_identity_key({"type": "file", "locator": {"path": "a/b.py"}})
        # different collections do NOT collapse
        assert content_identity_key({"type": "collection", "locator": {"id": "x"}}) \
            != content_identity_key({"type": "collection", "locator": {"id": "y"}})

    def test_dedupe_keeps_first_id_newest_ts_freshest_why(self):
        items = [
            {"id": "keep", "type": "collection", "locator": {"id": "c1"}, "ts": 10.0, "why": "old"},
            {"id": "dropme", "type": "collection", "locator": {"id": "c1"}, "ts": 20.0, "why": "new"},
        ]
        out = dedupe_content(items)
        assert len(out) == 1
        assert out[0]["id"] == "keep"          # first occurrence's stable id wins
        assert out[0]["ts"] == 20.0            # refreshed to the newest
        assert out[0]["why"] == "new"          # freshest non-empty why

    def test_dedupe_preserves_distinct_refs_and_order(self):
        items = [
            {"id": "a", "type": "file", "locator": {"path": "a.py"}, "ts": 1.0, "why": ""},
            {"id": "b", "type": "file", "locator": {"path": "b.py"}, "ts": 1.0, "why": ""},
            {"id": "a2", "type": "file", "locator": {"path": "a.py"}, "ts": 2.0, "why": "x"},
        ]
        out = dedupe_content(items)
        assert [it["id"] for it in out] == ["a", "b"]
        assert out[0]["ts"] == 2.0 and out[0]["why"] == "x"

    def test_add_content_merges_duplicate_reference_on_real_store(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        store = FileContextStore(str(cards_dir), confidence_threshold=0.0)
        ref = {"type": "collection", "locator": {"id": "col-9", "name": "Tiers"}, "why": "first"}
        store.add_content("topic-card", ref)
        store.add_content("topic-card", {**ref, "why": "second"})
        card = json.loads((cards_dir / "topic-card.json").read_text(encoding="utf-8"))
        cols = [c for c in card["content"] if c.get("type") == "collection"]
        assert len(cols) == 1                  # merged, not duplicated
        assert cols[0]["why"] == "second"      # freshest why retained
