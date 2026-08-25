"""quest_folder_zones — the three-zone provenance convention inside a synced quest folder.

Pure filesystem, no network: every test runs against a tempdir.
"""
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.runner.quest_folder_zones import (
    AI_DRIVEN_DIR,
    GUIDE_FILE,
    HUMAN_CONTEXT_DIR,
    INBOUND_DIR,
    LEDGER_NAME,
    LEDGER_STATUSES,
    capture_human_input,
    ensure_folder_zones,
    folder_zones_contract,
    is_human_note,
)


@pytest.fixture
def folder():
    with tempfile.TemporaryDirectory() as d:
        yield d


# --- scaffolding -------------------------------------------------------------

def test_ensure_creates_both_zones_the_ledger_and_the_guide(folder):
    zones = ensure_folder_zones(folder)
    base = Path(folder)
    assert (base / AI_DRIVEN_DIR).is_dir()
    assert (base / HUMAN_CONTEXT_DIR).is_dir()
    assert (base / HUMAN_CONTEXT_DIR / INBOUND_DIR).is_dir()
    assert (base / AI_DRIVEN_DIR / LEDGER_NAME).is_file()
    assert (base / GUIDE_FILE).is_file()
    assert zones.scaffolded


def test_ensure_is_idempotent(folder):
    ensure_folder_zones(folder)
    before = (Path(folder) / GUIDE_FILE).read_text()
    second = ensure_folder_zones(folder)
    assert second.created == []
    assert not second.scaffolded
    assert (Path(folder) / GUIDE_FILE).read_text() == before


def test_ensure_preserves_existing_claude_md_prose(folder):
    guide = Path(folder) / GUIDE_FILE
    guide.write_text("# My folder\n\nMy own rules, which are load-bearing.\n")
    ensure_folder_zones(folder)
    text = guide.read_text()
    assert "My own rules, which are load-bearing." in text
    assert AI_DRIVEN_DIR in text
    assert HUMAN_CONTEXT_DIR in text


def test_ledger_scaffold_is_empty_of_rows(folder):
    """A scaffolded row would be the library inventing a decision."""
    ensure_folder_zones(folder)
    ledger = (Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME).read_text()
    lines = [ln for ln in ledger.splitlines() if ln.strip().startswith("|")]
    assert len(lines) == 2                         # a header row and its separator...
    assert "Item" in lines[0] and "Status" in lines[0]
    assert set(lines[1].replace("|", "").split()) == {"---"}   # ...and nothing under them


def test_guide_states_only_approved_is_settled(folder):
    ensure_folder_zones(folder)
    guide = (Path(folder) / GUIDE_FILE).read_text()
    for status in LEDGER_STATUSES:
        assert status in guide
    assert "Only `approved` is settled." in guide


def test_ensure_survives_an_unwritable_folder():
    """A folder that cannot be scaffolded is a degraded convention, not a failed run."""
    zones = ensure_folder_zones("/proc/nonexistent-quest-folder/nope")
    assert zones.created == []


# --- who wrote it ------------------------------------------------------------

@pytest.mark.parametrize("note", [
    {"author_kind": "human"},
    {"author_kind": "USER"},
    {"author_kind": "person"},
    {"source": "email"},
    {"author_kind": "", "source": "reply"},
])
def test_human_notes_are_recognised(note):
    assert is_human_note(note)


@pytest.mark.parametrize("note", [
    {"author_kind": "ai"},
    {"author_kind": "AI", "source": "email"},   # an AI note is AI even if it claims a human source
    {},                                          # unknown is NOT human -- deliberately
    {"author_name": "Some Person"},              # the account name says nothing about the author
])
def test_non_human_notes_are_not_captured_as_human(note):
    assert not is_human_note(note)


# --- capturing their words ---------------------------------------------------

NOTES = [
    {"note_id": "note_a1", "text": "I haven't even read what the gaps are.",
     "created_at": "2026-08-19T10:00:00Z", "author_kind": "human"},
    {"note_id": "note_b2", "text": "Gap 2 resolved, verified.",
     "created_at": "2026-08-19T11:00:00Z", "author_kind": "ai", "author_name": "Some Person"},
    {"note_id": "note_c3", "text": "Replying by mail.",
     "created_at": "2026-08-20T09:00:00Z", "source": "email"},
]


def test_capture_writes_only_the_human_notes(folder):
    written = capture_human_input(folder, NOTES)
    assert len(written) == 2
    inbound = Path(folder) / HUMAN_CONTEXT_DIR / INBOUND_DIR
    captured = "\n".join(p.read_text() for p in inbound.iterdir())
    assert "I haven't even read what the gaps are." in captured
    assert "Gap 2 resolved" not in captured


def test_capture_preserves_their_words_exactly(folder):
    exact = "line one\n\n  indented, with *asterisks* and a trailing space \nlast line"
    capture_human_input(folder, [{"note_id": "n1", "text": exact, "author_kind": "human"}])
    path = next((Path(folder) / HUMAN_CONTEXT_DIR / INBOUND_DIR).iterdir())
    assert exact in path.read_text()


def test_capture_is_idempotent_across_pulls(folder):
    assert len(capture_human_input(folder, NOTES)) == 2
    assert capture_human_input(folder, NOTES) == []
    inbound = Path(folder) / HUMAN_CONTEXT_DIR / INBOUND_DIR
    assert len(list(inbound.iterdir())) == 2


def test_capture_skips_empty_notes(folder):
    assert capture_human_input(folder, [{"note_id": "n", "text": "   ", "author_kind": "human"}]) == []


def test_capture_records_provenance_in_the_frontmatter(folder):
    capture_human_input(folder, [NOTES[2]])
    path = next((Path(folder) / HUMAN_CONTEXT_DIR / INBOUND_DIR).iterdir())
    text = path.read_text()
    assert "note_id: note_c3" in text
    assert "arrived_by: email" in text
    assert "Do not edit" in text


def test_capture_filename_is_filesystem_safe(folder):
    capture_human_input(folder, [
        {"note_id": "../../escape", "text": "hi", "author_kind": "human"}])
    inbound = Path(folder) / HUMAN_CONTEXT_DIR / INBOUND_DIR
    names = [p.name for p in inbound.iterdir()]
    assert names and all(
        "/" not in n and ".." not in n and not n.startswith(".") for n in names)


# --- what the run is told ----------------------------------------------------

def test_contract_names_the_zones_and_the_ledger():
    text = folder_zones_contract("/some/folder")
    assert AI_DRIVEN_DIR in text and HUMAN_CONTEXT_DIR in text
    assert LEDGER_NAME in text
    assert "/some/folder" in text
    assert "ai_proposed" in text and "approved" in text
