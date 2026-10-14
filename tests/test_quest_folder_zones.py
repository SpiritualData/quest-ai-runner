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
    SOURCE_OF_TRUTH_DIR,
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
    assert (base / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR).is_dir()
    assert (base / AI_DRIVEN_DIR / LEDGER_NAME).is_file()
    assert (base / GUIDE_FILE).is_file()
    assert zones.scaffolded
    assert zones.source_of_truth == str(base / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR)


def test_source_of_truth_is_nested_inside_ai_driven_not_a_new_top_level_zone(folder):
    ensure_folder_zones(folder)
    base = Path(folder)
    assert not (base / SOURCE_OF_TRUTH_DIR).exists()
    assert (base / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR).is_dir()


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


def _rows_below_header(ledger_text):
    """Table lines after the managed rules header, i.e. the rows part of the ledger."""
    below = ledger_text.split("QAR:MANAGED:ledger_rules END -->", 1)[1]
    return [ln for ln in below.splitlines() if ln.strip().startswith("|")]


def test_ledger_scaffold_is_empty_of_rows(folder):
    """A scaffolded row would be the library inventing a decision."""
    ensure_folder_zones(folder)
    ledger = (Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME).read_text()
    lines = _rows_below_header(ledger)
    assert len(lines) == 2                         # a header row and its separator...
    assert "Item" in lines[0] and "Status" in lines[0]
    assert set(lines[1].replace("|", "").split()) == {"---"}   # ...and nothing under them


def test_ledger_header_holds_the_statuses_and_rules(folder):
    ensure_folder_zones(folder)
    ledger = (Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME).read_text()
    for status in LEDGER_STATUSES:
        assert f"`{status}`" in ledger
    assert "Only `approved` is settled." in ledger
    assert "Add a row when you propose" in ledger
    assert "only when you can quote them" in ledger


def test_guide_is_a_short_map_without_the_detail(folder):
    ensure_folder_zones(folder, quest_id="quest_demo", quest_name="Demo quest")
    guide = (Path(folder) / GUIDE_FILE).read_text()
    assert len(guide.splitlines()) <= 20  # markers included
    assert 'working folder of quest `quest_demo` ("Demo quest").' in guide
    assert "quest-ai-runner sync-quest-folder quest_demo . --direction both" in guide
    for item in ("QUEST_SYNC.md", f"{HUMAN_CONTEXT_DIR}/", f"{AI_DRIVEN_DIR}/",
                 f"{AI_DRIVEN_DIR}/{SOURCE_OF_TRUTH_DIR}/", f"{AI_DRIVEN_DIR}/{LEDGER_NAME}",
                 "everything else"):
        assert item in guide
    assert "Notes to push to Quest" in guide
    assert "|" not in guide                      # the status table lives in the ledger now
    assert "\u2014" not in guide


def test_guide_without_quest_info_omits_the_opening_line(folder):
    ensure_folder_zones(folder)
    guide = (Path(folder) / GUIDE_FILE).read_text()
    assert "working folder of quest" not in guide
    assert "sync-quest-folder <quest_id> ." in guide


def test_quest_name_cannot_inject_markers_or_newlines(folder):
    ensure_folder_zones(folder, quest_id="q1",
                        quest_name='Bad\nname <!-- QAR:MANAGED:x START --> tail')
    guide = (Path(folder) / GUIDE_FILE).read_text()
    assert "QAR:MANAGED:x" not in guide
    assert guide.splitlines()[1].startswith("This folder is the working folder of quest `q1`")
    assert guide.splitlines()[1].endswith('tail").')


def test_source_of_truth_readme_is_created_with_the_rules(folder):
    ensure_folder_zones(folder)
    readme = (Path(folder) / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR / "README.md").read_text()
    assert "No date in the filename" in readme
    assert "Check here before researching" in readme
    assert "named headings, not numbered" in readme


def test_source_of_truth_readme_keeps_existing_prose(folder):
    sot = Path(folder) / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR
    sot.mkdir(parents=True)
    (sot / "README.md").write_text("# Mine\n\nHand-written intro.\n")
    ensure_folder_zones(folder)
    text = (sot / "README.md").read_text()
    assert text.startswith("# Mine\n\nHand-written intro.\n")
    assert "No date in the filename" in text


def test_source_of_truth_readme_block_is_replaced_in_place(folder):
    ensure_folder_zones(folder)
    readme = Path(folder) / AI_DRIVEN_DIR / SOURCE_OF_TRUTH_DIR / "README.md"
    stale = readme.read_text().replace("No date in the filename", "STALE RULE")
    readme.write_text(stale + "\nTrailing human note.\n")
    ensure_folder_zones(folder)
    text = readme.read_text()
    assert "STALE RULE" not in text and "No date in the filename" in text
    assert text.endswith("Trailing human note.\n")


LEGACY_LEDGER = (
    "# Provenance ledger\n\nOld hand-written intro.\n\n"
    "| Item | Raised | Origin | Status | Their words, and where | Where it lives |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
    "| gap list | 2026-08-01 | AI | approved | \"go ahead\" (note n1) | notes.md |\n"
)


def test_header_is_inserted_into_an_existing_ledger_keeping_every_row(folder):
    ledger = Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEGACY_LEDGER)
    ensure_folder_zones(folder)
    text = ledger.read_text()
    assert text.startswith("# Provenance ledger\n")
    assert text.index("QAR:MANAGED:ledger_rules START") < text.index("Old hand-written intro.")
    assert "| gap list | 2026-08-01 | AI | approved | \"go ahead\" (note n1) | notes.md |" in text
    # everything the file said before is still there, in order, byte for byte
    after_title = LEGACY_LEDGER.split("\n", 1)[1].lstrip("\n")
    assert text.endswith(after_title)


def test_header_is_replaced_in_place_and_rows_are_untouched(folder):
    ensure_folder_zones(folder)
    ledger = Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME
    row = "| plan | 2026-09-01 | AI | surfaced | | plan.md |\n"
    ledger.write_text(ledger.read_text().replace("May you build on it?", "STALE") + row)
    ensure_folder_zones(folder)
    text = ledger.read_text()
    assert "STALE" not in text and "May you build on it?" in text
    assert text.endswith(row)


def test_ledger_without_a_title_gets_the_header_on_top(folder):
    ledger = Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME
    ledger.parent.mkdir(parents=True)
    ledger.write_text("| a | b |\n| --- | --- |\n| x | y |\n")
    ensure_folder_zones(folder)
    text = ledger.read_text()
    assert text.startswith("<!-- QAR:MANAGED:ledger_rules START -->")
    assert text.endswith("| a | b |\n| --- | --- |\n| x | y |\n")


def test_ensure_twice_yields_identical_files_including_with_legacy_input(folder):
    ledger = Path(folder) / AI_DRIVEN_DIR / LEDGER_NAME
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEGACY_LEDGER)
    ensure_folder_zones(folder, quest_id="q1", quest_name="N")
    snap = {p: p.read_text() for p in Path(folder).rglob("*.md")}
    second = ensure_folder_zones(folder, quest_id="q1", quest_name="N")
    assert second.created == []
    assert {p: p.read_text() for p in Path(folder).rglob("*.md")} == snap


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


def test_contract_shares_wording_with_the_rendered_files(folder):
    from quest_ai_runner.runner import quest_folder_zones as z
    text = folder_zones_contract()
    ensure_folder_zones(folder)
    guide = (Path(folder) / GUIDE_FILE).read_text()
    for shared in (z.MAP_HUMAN, z.MAP_AI, z.MAP_SOT, z.MAP_OTHER):
        assert shared in text and shared in guide
    assert z.RULE_APPROVE_ROW in text
    assert "\u2014" not in text
