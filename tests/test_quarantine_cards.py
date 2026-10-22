"""Tests for scripts/quarantine_cards.py, the card-store cleanup tool.

What these pin down is mostly what the tool must NOT do. It runs against a real store holding a
corpus's whole index, so the dangerous failures are: deleting instead of moving, moving a card
another system owns, moving a hand-learned card, and moving so much that the store is emptied.
Every case below is one of those.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "quarantine_cards.py"


def load_tool():
    """Import the script by path (it lives in scripts/, which is not a package)."""
    spec = importlib.util.spec_from_file_location("quarantine_cards_tool", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()


def write_card(cards_dir: Path, card_id: str, **fields) -> Path:
    card = {
        "id": card_id,
        "name": fields.pop("name", card_id),
        "files": [{"path": p} for p in fields.pop("files", [])],
        "usage_count": fields.pop("usage_count", 0),
        "provenance": fields.pop("provenance", {"created_by_task": "bootstrap"}),
    }
    card.update(fields)
    path = cards_dir / f"{card_id}.json"
    path.write_text(json.dumps(card), encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    cards = tmp_path / ".quest-context"
    cards.mkdir()
    return cards


# --- which ids count as compounded import copies ------------------------------------------
def test_an_interior_root_hop_is_compounded():
    # The shape that produced 26,761 copies in one real store: a store imported another store's
    # own imports, so the import namespace appears again inside the id.
    assert tool.compounding_evidence("repo-aaaa--root-bbbb--root-cccc--topic")


def test_a_repeated_namespace_segment_is_compounded():
    assert tool.compounding_evidence("root-aaaa--proj-bbbb--root-aaaa--topic")


def test_a_single_import_namespace_is_not_compounded():
    # One level of import is legitimate reuse, not compounding, and must survive.
    assert tool.compounding_evidence("root-aaaa--topic") == ""
    assert tool.compounding_evidence("plain-topic-card") == ""


def test_two_import_namespace_prefixes_are_compounded_but_one_is_not():
    assert tool.compounding_evidence("hq-dfbfcd--assistant-task-followup") == ""
    evidence = tool.compounding_evidence("root-5ec1f7--hq-dfbfcd--assistant-task-followup")
    assert evidence and "2 import namespace prefixes" in evidence
    assert "3 import namespace prefixes" in tool.compounding_evidence(
        "root-5ec1f7--hq-dfbfcd--app-abc123--topic")


def test_a_hex_looking_topic_word_is_not_a_prefix():
    assert tool.compounding_evidence("hq-dfbfcd--topic-abc123") == ""


# --- what must never be moved ---------------------------------------------------------------
def test_a_card_owned_by_another_system_is_never_moved(store: Path):
    write_card(store, "quest-folder-abc", files=["data/one.json"], managed_by="quest_folder_sync")
    entries, skipped = tool.plan(tool.load_cards(store), reasons=set(tool.ALL_REASONS),
                                 excluded=[], threshold=0.8)
    assert entries == []
    assert any("managed" in why for why in skipped)


def test_a_used_card_is_kept_even_when_it_duplicates_another(store: Path):
    write_card(store, "kept-original", files=["docs/a.md", "docs/b.md"], usage_count=5)
    write_card(store, "used-duplicate", files=["docs/a.md", "docs/b.md"], usage_count=3)
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"surplus"},
                                 excluded=[], threshold=0.8)
    assert entries == []
    assert any("has been used" in why for why in skipped)


def test_a_hand_learned_card_is_kept(store: Path):
    write_card(store, "learned", files=["docs/a.md"],
               provenance={"created_by_task": "task-from-a-run"})
    entries, _ = tool.plan(tool.load_cards(store), reasons=set(tool.ALL_REASONS),
                           excluded=["docs"], threshold=0.8)
    assert entries == []


def test_a_compounded_copy_is_moved_even_when_used(store: Path):
    # The one override: a compounded card is a COPY, so whatever it carries also exists on the
    # card it was copied from. Nothing is lost by moving it.
    write_card(store, "a-aaaa--root-bbbb--root-cccc--topic", files=["docs/a.md"], usage_count=9)
    write_card(store, "hq-dfbfcd--topic", files=["docs/a.md"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"compounded"},
                           excluded=[], threshold=0.8)
    assert [e["reason"] for e in entries] == ["compounded"]


def test_a_compounded_card_with_a_surviving_equivalent_is_moved(store: Path):
    write_card(store, "a-aaaa--root-bbbb--root-cccc--topic", files=["docs/a.md", "docs/b.md"])
    write_card(store, "hq-dfbfcd--topic", files=["docs/a.md", "docs/b.md", "docs/c.md"])
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"compounded"},
                                 excluded=[], threshold=0.8)
    assert [(e["card_id"], e["reason"]) for e in entries] == [
        ("a-aaaa--root-bbbb--root-cccc--topic", "compounded")]
    assert "equivalent kept: hq-dfbfcd--topic" in entries[0]["detail"]
    assert skipped == {}


def test_a_compounded_only_copy_is_kept_and_listed_in_skipped(store: Path):
    write_card(store, "a-aaaa--root-bbbb--root-cccc--goals", files=["thesis/GOALS.md"])
    write_card(store, "unrelated", files=["docs/a.md", "docs/b.md"])
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"compounded"},
                                 excluded=[], threshold=0.8)
    assert entries == []
    assert skipped == {"compounded card kept: its only copy in this store": 1}


def test_a_compounded_card_whose_only_equivalent_is_moved_in_the_same_run_is_kept(store: Path):
    # The equivalent is mostly inside an excluded folder, so it is moved in this run; the
    # compounded card is not excluded itself, so moving it too would drop the file's coverage.
    write_card(store, "a-aaaa--root-bbbb--root-cccc--topic", files=["app/c.py"])
    write_card(store, "mostly-vendored", files=["vendor/a.py", "vendor/b.py", "app/c.py"])
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"compounded", "excluded"},
                                 excluded=["vendor"], threshold=0.8)
    assert [(e["card_id"], e["reason"]) for e in entries] == [("mostly-vendored", "excluded")]
    assert skipped == {"compounded card kept: its only copy in this store": 1}


def test_a_compounded_data_file_card_is_still_moved_as_thin_with_a_thin_equivalent(store: Path):
    # A card that is itself thin goes under that reason as before; its equivalent, also thin,
    # goes too, so neither is counted as a surviving copy.
    write_card(store, "a-aaaa--root-bbbb--root-cccc--state", files=["app/run_state.json"])
    write_card(store, "state-card", files=["app/run_state.json"])
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"compounded", "thin"},
                                 excluded=[], threshold=0.8)
    assert sorted((e["card_id"], e["reason"]) for e in entries) == [
        ("a-aaaa--root-bbbb--root-cccc--state", "thin"), ("state-card", "thin")]
    assert skipped == {}


def test_a_compounded_card_inside_an_excluded_folder_is_still_moved(store: Path):
    write_card(store, "a-aaaa--root-bbbb--root-cccc--vendored", files=["vendor/a.py", "vendor/b.py"])
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"compounded", "excluded"},
                                 excluded=["vendor"], threshold=0.8)
    assert [e["reason"] for e in entries] == ["excluded"]
    assert skipped == {}


# --- the three remaining reasons ------------------------------------------------------------
def test_a_card_entirely_inside_an_excluded_folder_is_moved(store: Path):
    write_card(store, "vendored-topic", files=["vendor/pkg/a.py", "vendor/pkg/b.py"])
    write_card(store, "our-topic", files=["app/a.py", "vendor/pkg/b.py"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"excluded"},
                           excluded=["vendor/pkg"], threshold=0.8)
    # Only the card whose EVERY file is inside the excluded folder goes; a card that straddles it
    # still describes something in this corpus.
    assert [e["card_id"] for e in entries] == ["vendored-topic"]


def test_a_card_with_most_files_excluded_is_moved_unless_used_or_managed(store: Path):
    write_card(store, "mostly-vendored", files=["vendor/a.py", "vendor/b.py", "app/c.py"])
    write_card(store, "half-vendored", files=["vendor/a.py", "app/c.py"])
    write_card(store, "used-vendored", files=["vendor/a.py", "vendor/b.py"], usage_count=2)
    write_card(store, "managed-vendored", files=["vendor/a.py", "vendor/b.py"], managed_by="sync")
    entries, _ = tool.plan(tool.load_cards(store), reasons={"excluded"},
                           excluded=["vendor"], threshold=0.8)
    assert [e["card_id"] for e in entries] == ["mostly-vendored"]
    assert "2 of 3" in entries[0]["detail"]


def test_the_root_prefix_never_excludes_everything(store: Path):
    # "." is a prefix of every path written as "./x"; one such entry in the review once excluded
    # a whole corpus. The tool must ignore it like the store does.
    write_card(store, "topic", files=["app/a.py", "app/b.py"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"excluded"},
                           excluded=["."], threshold=0.8)
    assert entries == []


def test_surplus_duplicates_collapse_to_the_earliest_card(store: Path):
    write_card(store, "first", files=["docs/a.md", "docs/b.md"],
               provenance={"created_by_task": "bootstrap", "created_at": "2026-01-01"})
    write_card(store, "second", files=["docs/a.md", "docs/b.md"],
               provenance={"created_by_task": "bootstrap", "created_at": "2026-06-01"})
    write_card(store, "third", files=["docs/a.md", "docs/b.md"],
               provenance={"created_by_task": "bootstrap", "created_at": "2026-09-01"})
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus"},
                           excluded=[], threshold=0.8)
    assert sorted(e["card_id"] for e in entries) == ["second", "third"]
    assert all("same files as first" in e["detail"] for e in entries)


def test_surplus_keeper_prefers_usage_then_name_match_then_age_then_id(store: Path):
    files = ["billing/invoice_export.md", "billing/notes.md"]
    write_card(store, "a-vague", name="misc", files=files)
    write_card(store, "b-matching", name="billing invoice export", files=files)
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus"},
                           excluded=[], threshold=0.8)
    assert [(e["card_id"], e["detail"]) for e in entries] == [("a-vague", "same files as b-matching")]


def test_surplus_ties_fall_to_earliest_created_then_id(store: Path):
    files = ["docs/a.md", "docs/b.md"]
    write_card(store, "a-late", files=files, provenance={"created_by_task": "bootstrap",
                                                          "created_at": "2026-09-01"})
    write_card(store, "z-early", files=files, provenance={"created_by_task": "bootstrap",
                                                           "created_at": "2026-01-01"})
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus"},
                           excluded=[], threshold=0.8)
    assert [e["card_id"] for e in entries] == ["a-late"]
    write_card(store, "z-early", files=files)
    write_card(store, "a-late", files=files)
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus"},
                           excluded=[], threshold=0.8)
    assert [e["card_id"] for e in entries] == ["z-early"]


def test_name_match_score_is_a_share_of_card_words_found_in_paths():
    card = {"name": "billing report", "keywords": ["invoice"],
            "files": [{"path": "billing/report_final.md"}]}
    assert tool.name_match_score(card) == pytest.approx(2 / 3)
    assert tool.name_match_score({"files": []}) == 0.0


def test_surplus_detail_says_when_the_keeper_is_also_moved(store: Path):
    # The keeper is a single data file duplicated by another card, so it is also thin.
    write_card(store, "keeper-state", files=["app/run_state.json"], usage_count=0,
               provenance={"created_by_task": "bootstrap", "created_at": "2026-01-01"})
    write_card(store, "copy-state", files=["app/run_state.json"],
               provenance={"created_by_task": "bootstrap", "created_at": "2026-02-01"})
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus", "thin"},
                           excluded=[], threshold=0.8)
    by_id = {e["card_id"]: e for e in entries}
    assert "keeper-state is also moved: thin" in by_id["copy-state"]["detail"]
    assert (store / "keeper-state.json").read_text(encoding="utf-8")


def test_a_distinct_file_set_is_not_surplus(store: Path):
    write_card(store, "one", files=["docs/a.md", "docs/b.md"])
    write_card(store, "two", files=["docs/c.md", "docs/d.md"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"surplus"},
                           excluded=[], threshold=0.8)
    assert entries == []


def test_a_single_data_file_card_is_thin_but_a_markdown_one_is_not(store: Path):
    write_card(store, "state-card", files=["app/runner_state.json"])
    write_card(store, "note-card", files=["docs/design.md"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"thin"},
                           excluded=[], threshold=0.8)
    assert [e["card_id"] for e in entries] == ["state-card"]


# --- dry run, moving, manifest --------------------------------------------------------------
def test_dry_run_touches_nothing(store: Path, capsys):
    write_card(store, "state-card", files=["app/runner_state.json"])
    assert tool.main(["--cards-dir", str(store)]) == 0
    assert (store / "state-card.json").exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "nothing was moved" in out


def test_apply_moves_cards_instead_of_deleting_them(store: Path):
    write_card(store, "state-card", files=["app/runner_state.json"])
    write_card(store, "keeper", files=["docs/a.md", "docs/b.md"])
    assert tool.main(["--cards-dir", str(store), "--apply"]) == 0
    moved = store / tool.QUARANTINE_DIR_NAME / "thin" / "state-card.json"
    assert moved.exists(), "the card must be MOVED, never deleted"
    assert not (store / "state-card.json").exists()
    assert (store / "keeper.json").exists()


def test_the_quarantine_dir_is_invisible_to_the_card_store(store: Path):
    # The whole approach rests on this: if the store still loaded quarantined cards, moving them
    # would shrink nothing.
    from quest_ai_runner.adapters.card_repository import FilesystemCardRepository

    write_card(store, "state-card", files=["app/runner_state.json"])
    write_card(store, "keeper", files=["docs/a.md", "docs/b.md"])
    tool.main(["--cards-dir", str(store), "--apply"])
    assert set(FilesystemCardRepository(str(store)).load_all()) == {"keeper"}


def test_the_manifest_names_every_card_and_the_counts(store: Path, tmp_path: Path):
    write_card(store, "state-card", files=["app/runner_state.json"])
    write_card(store, "keeper", files=["docs/a.md", "docs/b.md"])
    manifest = tmp_path / "manifest.json"
    tool.main(["--cards-dir", str(store), "--manifest", str(manifest)])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["applied"] is False
    assert payload["cards_found"] == 2
    assert payload["cards_quarantined"] == 1
    assert payload["cards_remaining"] == 1
    assert [e["card_id"] for e in payload["entries"]] == ["state-card"]


def test_excluded_prefixes_are_read_from_the_review_with_the_current_rules(store: Path):
    (store / "folder_review.json").write_text(json.dumps({"version": 2, "folders": {
        # Final: a nested repository. Its kept child must not rescue it.
        "vendor/pkg": {"index": False, "final": True, "source": "nested_repo", "mixed": False},
        "vendor/pkg/docs": {"index": True, "mixed": False},
        # A mixed model skip IS superseded by a kept child.
        "workspace": {"index": False, "mixed": True},
        "workspace/notes": {"index": True, "mixed": False},
        # The root is never excludable.
        ".": {"index": False, "mixed": False},
    }}), encoding="utf-8")
    prefixes = tool.excluded_prefixes_from_review(store)
    assert "vendor/pkg" in prefixes
    assert "workspace" not in prefixes
    assert "." in prefixes  # carried, but under_any_prefix ignores it
    assert tool.under_any_prefix("anything/at/all", ["."]) is None


def test_an_unknown_reason_is_refused(store: Path):
    assert tool.main(["--cards-dir", str(store), "--reasons", "nonsense"]) == 2


def test_a_kept_prefix_rescues_a_reviewed_folder_but_not_an_explicit_exclusion_beneath_it(store: Path):
    # A nested repository the review excludes can still hold prose the owner wrote; --keep-prefix
    # keeps those cards, and --excluded-prefix beneath it still moves the vendored part.
    (store / "folder_review.json").write_text(json.dumps({"version": 2, "folders": {
        "org/project": {"index": False, "final": True, "source": "nested_repo", "mixed": False},
        "vendor": {"index": False, "final": True, "source": "nested_repo", "mixed": False},
    }}), encoding="utf-8")
    write_card(store, "owner-notes", files=["org/project/notes.md"])
    write_card(store, "vendored-model", files=["org/project/archive/model.py"])
    write_card(store, "vendor-lib", files=["vendor/lib.py"])
    assert tool.main(["--cards-dir", str(store), "--reasons", "excluded", "--apply",
                      "--keep-prefix", "org/project",
                      "--excluded-prefix", "org/project/archive"]) == 0
    left = {p.stem for p in store.glob("*.json") if p.stem != "folder_review"}
    assert left == {"owner-notes"}


# --- covered by another store ---------------------------------------------------------------
@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    cards = tmp_path / "shared-store"
    cards.mkdir()
    return cards


def test_covered_moves_identical_and_subset_cards_only(store: Path, shared: Path):
    write_card(shared, "big", files=["docs/a.md", "docs/b.md", "docs/c.md"])
    write_card(store, "same", files=["docs/a.md", "docs/b.md", "docs/c.md"])
    write_card(store, "inside", files=["docs/a.md", "docs/b.md"])
    write_card(store, "sticks-out", files=["docs/a.md", "docs/z.md"])
    entries, _ = tool.plan(tool.load_cards(store), reasons={"covered"}, excluded=[],
                           threshold=0.8, covered_by=[tool.load_cards(shared)])
    assert sorted(e["card_id"] for e in entries) == ["inside", "same"]
    assert all("big" in e["detail"] for e in entries)


def test_covered_compares_paths_after_stripping_a_prefix(store: Path, shared: Path):
    write_card(shared, "rooted-above", files=["corpus/docs/a.md", "corpus/docs/b.md"])
    write_card(store, "plain", files=["docs/a.md", "docs/b.md"])
    args = dict(reasons={"covered"}, excluded=[], threshold=0.8,
                covered_by=[tool.load_cards(shared)])
    assert tool.plan(tool.load_cards(store), **args)[0] == []
    entries, _ = tool.plan(tool.load_cards(store), strip_prefixes=["corpus"], **args)
    assert [e["card_id"] for e in entries] == ["plain"]


def test_covered_never_moves_protected_cards(store: Path, shared: Path):
    write_card(shared, "big", files=["docs/a.md", "docs/b.md"])
    write_card(store, "used", files=["docs/a.md"], usage_count=1)
    write_card(store, "learned", files=["docs/a.md"], provenance={"created_by_task": "a-run"})
    write_card(store, "owned", files=["docs/b.md"], managed_by="sync")
    entries, skipped = tool.plan(tool.load_cards(store), reasons={"covered"}, excluded=[],
                                 threshold=0.8, covered_by=[tool.load_cards(shared)])
    assert entries == []
    assert sum(skipped.values()) == 3


def test_covered_is_not_a_default_reason_and_needs_covered_by(store: Path, shared: Path):
    write_card(shared, "big", files=["docs/a.md", "docs/b.md"])
    write_card(store, "inside", files=["docs/a.md"])
    assert "covered" not in tool.DEFAULT_REASONS
    assert tool.main(["--cards-dir", str(store), "--covered-by", str(shared)]) == 0
    assert (store / "inside.json").exists()
    assert tool.main(["--cards-dir", str(store), "--reasons", "covered"]) == 2
    assert tool.main(["--cards-dir", str(store), "--reasons", "covered",
                      "--covered-by", str(store)]) == 2


def test_covered_applies_through_main_and_moves_instead_of_deleting(store: Path, shared: Path):
    write_card(shared, "big", files=["docs/a.md", "docs/b.md"])
    write_card(store, "inside", files=["docs/a.md"])
    assert tool.main(["--cards-dir", str(store), "--reasons", "covered",
                      "--covered-by", str(shared), "--apply"]) == 0
    assert (store / tool.QUARANTINE_DIR_NAME / "covered" / "inside.json").exists()
    assert (shared / "big.json").exists()
