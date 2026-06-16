"""Offline tests for NoteContextStore."""
import json
from pathlib import Path

import pytest

from quest_ai_runner.core.note_context_store import NoteContextStore
from quest_ai_runner.core.adapters import AssembledContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_note(nid: str, text: str, **kwargs) -> dict:
    return {"id": nid, "text": text, **kwargs}


# ---------------------------------------------------------------------------
# sync_from_notes — write / idempotency / prune
# ---------------------------------------------------------------------------


def test_sync_creates_cards(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    notes = [
        _make_note("n1", "be concise in status updates"),
        _make_note("n2", "never schedule meetings on Fridays"),
    ]
    store.sync_from_notes(notes)
    cards = list((tmp_path / "notes").glob("*.json"))
    assert len(cards) == 2


def test_sync_card_content(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise")])
    card_path = tmp_path / "notes" / "note_n1.json"
    assert card_path.exists()
    data = json.loads(card_path.read_text())
    assert data["id"] == "note_n1"
    assert data["text"] == "be concise"
    assert "description" in data
    assert data["source"] == "quest"


def test_sync_idempotent(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    notes = [_make_note("n1", "be concise")]
    store.sync_from_notes(notes)
    mtime1 = (tmp_path / "notes" / "note_n1.json").stat().st_mtime
    store.sync_from_notes(notes)  # same notes, same text
    mtime2 = (tmp_path / "notes" / "note_n1.json").stat().st_mtime
    assert mtime1 == mtime2  # file was not re-written


def test_sync_prunes_stale_cards(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "first"), _make_note("n2", "second")])
    assert len(list((tmp_path / "notes").glob("*.json"))) == 2
    # Re-sync with only n1 — n2 should be pruned.
    store.sync_from_notes([_make_note("n1", "first")])
    remaining = list((tmp_path / "notes").glob("*.json"))
    assert len(remaining) == 1
    assert remaining[0].stem == "note_n1"


def test_sync_empty_list_prunes_all(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "first")])
    store.sync_from_notes([])
    assert len(list((tmp_path / "notes").glob("*.json"))) == 0


def test_sync_skips_blank_text(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([{"id": "n1", "text": "  "}, {"id": "n2", "text": "valid"}])
    cards = list((tmp_path / "notes").glob("*.json"))
    assert len(cards) == 1
    assert cards[0].stem == "note_n2"


def test_sync_note_without_id_uses_sha1(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([{"text": "no id note"}])
    cards = list((tmp_path / "notes").glob("*.json"))
    assert len(cards) == 1
    # Should be a sha1-derived id, not crash
    data = json.loads(cards[0].read_text())
    assert data["id"].startswith("note_")


def test_sync_source_field_forwarded(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "note text", source="custom")])
    data = json.loads((tmp_path / "notes" / "note_n1.json").read_text())
    assert data["source"] == "custom"


def test_sync_never_raises_on_bad_dir(tmp_path):
    # notes_dir points to an existing *file*, not a dir — should not raise
    bad_path = tmp_path / "badfile"
    bad_path.write_text("not a dir")
    store = NoteContextStore(str(bad_path))
    store.sync_from_notes([_make_note("n1", "text")])  # should not raise


# ---------------------------------------------------------------------------
# assemble — retrieval / floor / ordering
# ---------------------------------------------------------------------------


def test_assemble_empty_returns_empty(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    result = store.assemble("anything")
    assert isinstance(result, AssembledContext)
    assert result.context_view == ""


def test_assemble_after_sync_returns_corrections(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise in updates")])
    result = store.assemble("write a status update")
    assert "be concise in updates" in result.context_view


def test_assemble_includes_correction_header(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise")])
    result = store.assemble("anything")
    assert "LEARNED CORRECTIONS" in result.context_view


def test_assemble_recent_floor_always_included(tmp_path):
    """The 2 most recent notes are always included regardless of keyword overlap."""
    store = NoteContextStore(str(tmp_path / "notes"))
    # Add 5 notes; the last 2 should always appear even on an unrelated query.
    for i in range(1, 6):
        store.sync_from_notes([_make_note(f"n{j}", f"note topic{j}") for j in range(1, i + 1)])
    result = store.assemble("xyzzy completely unrelated query")
    # topic4 and topic5 are the most recent two — must always appear.
    assert "topic4" in result.context_view
    assert "topic5" in result.context_view


def test_assemble_max_total_respected(tmp_path):
    """assemble() returns at most 6 corrections."""
    store = NoteContextStore(str(tmp_path / "notes"))
    notes = [_make_note(f"n{i}", f"correction keyword{i}") for i in range(1, 12)]
    store.sync_from_notes(notes)
    result = store.assemble("keyword1 keyword2 keyword3 keyword4 keyword5 keyword6 keyword7")
    bullets = [line for line in result.context_view.splitlines() if line.startswith("- ")]
    assert len(bullets) <= 6


def test_assemble_keyword_match_selects_relevant(tmp_path):
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([
        _make_note("n1", "be concise in python code reviews"),
        _make_note("n2", "always greet in spanish"),
        _make_note("n3", "latest unrelated note"),
    ])
    result = store.assemble("python code style")
    assert "concise in python" in result.context_view


def test_assemble_never_raises_on_corrupt_card(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "bad.json").write_text("not valid json{{{")
    store = NoteContextStore(str(notes_dir))
    result = store.assemble("anything")
    assert isinstance(result, AssembledContext)


# ---------------------------------------------------------------------------
# record — no-op contract
# ---------------------------------------------------------------------------


def test_record_is_noop(tmp_path):
    """record() must not create any card files and must never raise."""
    store = NoteContextStore(str(tmp_path / "notes"))
    store.record("some task", {"response": "some result"})
    # No card files written
    if (tmp_path / "notes").exists():
        assert len(list((tmp_path / "notes").glob("*.json"))) == 0
