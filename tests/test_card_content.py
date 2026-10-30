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

from quest_ai_runner.adapters.file_context_store import FileContextStore
from hubofhubs.recall.context_card_extraction import _trim_content_by_recency
from hubofhubs.ports.context import AssembledContext




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


class TestContentHelpers:
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
# Reference DEDUP on a real store
# ---------------------------------------------------------------------------

class TestContentDedup:
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


# ---------------------------------------------------------------------------
# Recency/usage RANKING boost (keyword arm): a recently-used or recently-updated
# card wins a near-tie, but the confidence gate still uses the un-boosted score.
# ---------------------------------------------------------------------------

class TestRecencyBoost:
    def _store(self, tmp_path, **kw):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        kw.setdefault("confidence_threshold", 0.0)
        return FileContextStore(str(cards_dir), **kw), cards_dir

    def test_no_history_card_factor_is_one(self, tmp_path):
        store, _ = self._store(tmp_path)
        card = {"id": "x", "usage_count": 0, "content": []}
        assert store._recency_boost_factor(card) == 1.0

    def test_recent_and_used_card_is_boosted_within_cap(self, tmp_path):
        store, _ = self._store(tmp_path, recency_boost_max=0.20)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        card = {"id": "x", "usage_count": 10,
                "content": [{"id": "i", "type": "note", "locator": {"text": "t"}, "ts": now}]}
        f = store._recency_boost_factor(card)
        assert 1.0 < f <= 1.20 + 1e-9          # boosted, but never beyond the cap

    def test_boost_disabled_returns_one(self, tmp_path):
        store, _ = self._store(tmp_path, recency_boost_max=0.0)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        card = {"id": "x", "usage_count": 99,
                "content": [{"id": "i", "type": "note", "locator": {"text": "t"}, "ts": now}]}
        assert store._recency_boost_factor(card) == 1.0

    def test_boost_breaks_near_tie_but_gate_uses_unboosted_score(self, tmp_path):
        # Two cards share the same keyword (equal relevance). One has usage + fresh content, so it
        # ranks first. A third card shares NO keyword: the boost must not pull it in past the gate.
        store, cards_dir = self._store(tmp_path, confidence_threshold=0.5, recency_boost_max=0.20)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        _write(cards_dir, _content_card("plain", keywords=["alpha"], content=[]))
        boosted = _content_card("boosted", keywords=["alpha"],
                                content=[{"id": "i", "type": "note", "locator": {"text": "t"},
                                          "ts": now, "why": ""}])
        boosted["usage_count"] = 5
        _write(cards_dir, boosted)
        _write(cards_dir, _content_card("offtopic", keywords=["zeta"], content=[]))
        ac = store.assemble("alpha")
        view = ac.context_view
        # The off-topic card is gated out (its un-boosted relevance is below the floor).
        assert "offtopic" not in view
        # Both alpha cards are present; the boosted one is ranked ahead of the plain one.
        assert "boosted" in view and "plain" in view
        assert view.index("boosted") < view.index("plain")
