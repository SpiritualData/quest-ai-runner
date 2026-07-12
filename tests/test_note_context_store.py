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
    # "update" vs "updates" is a trivial inflection: the gate's lenient (stemmed) matching must
    # not drop the correction over it.
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise in updates")])
    result = store.assemble("write a status update")
    assert "be concise in updates" in result.context_view


def test_assemble_includes_correction_header(tmp_path):
    # The note was just synced (fresh created_at): the freshness bypass keeps it floored even
    # though "anything" shares no keyword with it.
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise")])
    result = store.assemble("anything")
    assert "LEARNED CORRECTIONS" in result.context_view


# ---------------------------------------------------------------------------
# assemble — the relevance-gated recent floor
# ---------------------------------------------------------------------------


def _five_topic_notes():
    """Five notes with disjoint topics, explicitly timestamped so n4/n5 are the most recent."""
    return [
        _make_note(f"n{j}", f"note topic{j}", created_at=f"2026-01-01T00:00:0{j}Z")
        for j in range(1, 6)
    ]


def test_floor_gate_drops_unrelated_recent_notes(tmp_path):
    """Default behavior: a clearly unrelated query no longer drags in the 2 most recent notes."""
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes(_five_topic_notes())
    result = store.assemble("xyzzy completely unrelated query")
    # No note relates to the query, floor included: assemble returns an EMPTY context.
    assert result.context_view == ""


def test_floor_gate_keeps_related_recent_note(tmp_path):
    """A recent note that shares even one meaningful keyword with the query still floors in."""
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes(_five_topic_notes())
    result = store.assemble("tell me about topic5 xyzzy")
    assert "topic5" in result.context_view
    # The OTHER recent note (topic4) shares nothing with the query — gated out.
    assert "topic4" not in result.context_view


def test_floor_gate_escape_hatch_env(tmp_path, monkeypatch):
    """QAR_NOTE_FLOOR_RELEVANCE_GATED=0 restores the old unconditional floor."""
    monkeypatch.setenv("QAR_NOTE_FLOOR_RELEVANCE_GATED", "0")
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes(_five_topic_notes())
    result = store.assemble("xyzzy completely unrelated query")
    assert "topic4" in result.context_view
    assert "topic5" in result.context_view


def test_floor_gate_lenient_to_inflection(tmp_path):
    """The gate compares stemmed forms: a singular/plural (or -ing/-ed) mismatch between the
    query and an otherwise applicable correction must not drop it. Old timestamp, so the
    freshness bypass plays no part."""
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([
        _make_note("n1", "be concise in updates", created_at="2026-01-01T00:00:01Z"),
    ])
    result = store.assemble("write a status update")
    assert "be concise in updates" in result.context_view


def test_floor_gate_ranking_stays_exact_token(tmp_path):
    """Leniency is gate-only: ranked selection still scores by exact keyword overlap, so a
    non-floored note whose only relation is an inflection keeps score 0 and stays out."""
    store = NoteContextStore(str(tmp_path / "notes"))
    notes = [_make_note("n1", "be concise in updates", created_at="2026-01-01T00:00:01Z")]
    # Two newer notes occupy the whole floor; n1 can only enter via ranked selection.
    notes += [
        _make_note(f"n{j}", f"note topic{j}", created_at=f"2026-01-01T00:00:0{j}Z")
        for j in (2, 3)
    ]
    store.sync_from_notes(notes)
    result = store.assemble("write a status update")
    assert "be concise in updates" not in result.context_view


def test_floor_gate_fresh_note_bypasses_gate(tmp_path):
    """A note learned within the freshness window floors in even on a keyword-unrelated query:
    a just-given correction is almost certainly still in-topic."""
    store = NoteContextStore(str(tmp_path / "notes"))
    # No created_at on the note: sync stamps it with now, inside the 60-minute window.
    store.sync_from_notes([_make_note("n1", "be concise")])
    result = store.assemble("xyzzy completely unrelated query")
    assert "be concise" in result.context_view


def test_floor_gate_freshness_window_env_override(tmp_path, monkeypatch):
    """QAR_NOTE_FLOOR_FRESH_MINUTES=0 disables the freshness bypass: the same just-synced,
    unrelated note is gated out on keywords alone."""
    monkeypatch.setenv("QAR_NOTE_FLOOR_FRESH_MINUTES", "0")
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes([_make_note("n1", "be concise")])
    result = store.assemble("xyzzy completely unrelated query")
    assert result.context_view == ""


def test_floor_kept_when_query_has_no_keywords(tmp_path):
    """Permissive bar: a query with no meaningful keywords cannot judge relevance — keep floor."""
    store = NoteContextStore(str(tmp_path / "notes"))
    store.sync_from_notes(_five_topic_notes())
    result = store.assemble("ok?")
    assert "topic4" in result.context_view
    assert "topic5" in result.context_view


def test_floor_bypasses_ranking_when_relevant(tmp_path):
    """A relevant recent note floors in even when higher-scoring notes would fill every slot."""
    store = NoteContextStore(str(tmp_path / "notes"))
    notes = [
        _make_note(f"n{i}", f"deploy server rule number{i}",
                   created_at=f"2026-01-01T00:00:0{i}Z")
        for i in range(1, 7)  # six notes scoring 2 against the query below
    ]
    notes += [
        _make_note("n7", "deploy quietly overnight", created_at="2026-01-01T00:00:07Z"),
        _make_note("n8", "deploy without fanfare", created_at="2026-01-01T00:00:08Z"),
    ]
    store.sync_from_notes(notes)
    result = store.assemble("deploy server config")
    # n7/n8 score only 1 and would lose every ranked slot to the six score-2 notes, but they
    # are recent AND relevant, so the floor guarantees them a place.
    assert "deploy quietly overnight" in result.context_view
    assert "deploy without fanfare" in result.context_view


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
