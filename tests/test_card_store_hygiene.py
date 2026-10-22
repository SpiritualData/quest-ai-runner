"""Tidying a card store: only what is PROVABLY redundant moves, it moves and is never deleted, and the store
says so once. The judgement calls (excluded folders, overlap, thin cards) stay with scripts/quarantine_cards.py.

Everything is offline: tmp folders, no model.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from quest_ai_runner.adapters import card_limit, card_store_hygiene as hygiene
from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))

# Canonical-looking namespace prefixes (words, then six hex digits), as the import step makes them.
SD, PRODUCT, ROOT = "sd-aaaaaa", "product-bbbbbb", "root-cccccc"


def card(card_id: str, files: List[str] = (), **extra: Any) -> Dict[str, Any]:
    # A copy carries the name of the card it copies, so the default name is the id without its import prefixes.
    out = {"id": card_id, "name": card_id.split("--")[-1].replace("card:", "", 1), "summary": f"about {card_id}", "keywords": [card_id],
           "files": [{"path": p} for p in files], "usage_count": 0,
           "provenance": {"created_by_task": "bootstrap"}}
    out.update(extra)
    return out


def seed(cards_dir: Path, cards: List[Dict[str, Any]]) -> None:
    """Card files as an older version left them: written straight to disk, because the repository now
    refuses the id shapes this exists to clean up."""
    cards_dir.mkdir(parents=True, exist_ok=True)
    for c in cards:
        (cards_dir / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")


def ids(cards_dir: Path) -> List[str]:
    return sorted(FilesystemCardRepository(str(cards_dir)).load_all())


def moved(cards_dir: Path, reason: str) -> List[str]:
    folder = cards_dir / "_quarantine" / reason
    return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []


def plan_and_apply(cards_dir: Path):
    plan = hygiene.plan_tidy(hygiene.card_files_in(cards_dir))
    return plan, hygiene.apply_tidy(cards_dir, plan)


def store(tmp_path: Path, **kwargs: Any) -> FileContextStore:
    kwargs.setdefault("max_cards", 0)
    return FileContextStore(str(tmp_path / "cards"), repo_root=str(tmp_path), auto_bootstrap=False,
                            mount_mode="copy", **kwargs)


# --- what is provable --------------------------------------------------------------------------------

class TestCompoundedCopies:
    def test_a_compounded_copy_of_a_card_the_store_holds_is_moved_and_the_card_stays(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card(f"{SD}--auth-topic", files), card(f"{SD}--{PRODUCT}--auth-topic", files),
                     card(f"{ROOT}--{SD}--{ROOT}--auth-topic", files)])
        plan, result = plan_and_apply(cards)
        assert result.moved == 2 and result.by_reason == {"compounded": 2}
        assert ids(cards) == [f"{SD}--auth-topic"], "one import prefix is a normal single-hop copy"
        assert moved(cards, "compounded") == sorted([f"{SD}--{PRODUCT}--auth-topic",
                                                     f"{ROOT}--{SD}--{ROOT}--auth-topic"])

    def test_a_copy_that_pins_the_files_of_the_stores_own_card_is_moved_as_a_duplicate(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("auth-topic", files), card(f"{SD}--auth-topic", files)])
        assert plan_and_apply(cards)[1].by_reason == {"duplicate-files": 1}
        assert ids(cards) == ["auth-topic"]

    def test_the_only_copy_of_a_card_is_never_moved(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card(f"{SD}--{PRODUCT}--lonely", ["x/one.md", "x/two.md"])])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and plan.compounded_left == 1
        assert ids(cards) == [f"{SD}--{PRODUCT}--lonely"]

    def test_several_compounded_copies_of_one_card_keep_the_one_with_fewest_prefixes(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["x/one.md", "x/two.md"]
        seed(cards, [card(f"{SD}--{PRODUCT}--t", files), card(f"{SD}--{PRODUCT}--{ROOT}--t", files)])
        plan_and_apply(cards)
        assert ids(cards) == [f"{SD}--{PRODUCT}--t"]

    def test_notes_a_copy_gained_join_the_card_it_copies_before_the_copy_moves(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        note = {"id": "note-1", "type": "note", "locator": {"text": "learned here"}}
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files, name="t", summary="about t",
                                            content=[note], scope_tags=["quest:q1"], usage_count=3)])
        plan, result = plan_and_apply(cards)
        assert result.moved == 1 and moved(cards, "compounded") == [f"{SD}--{PRODUCT}--t"]
        kept = FilesystemCardRepository(str(cards)).read("t")
        assert [i["id"] for i in kept["content"]] == ["note-1"]
        assert kept["scope_tags"] == ["quest:q1"] and kept["usage_count"] == 3

    def test_a_copy_that_cannot_join_without_loss_stays_and_is_counted(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        note = {"id": "note-1", "type": "note", "locator": {"text": "learned here"}}
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files, content=[note])])   # its summary was edited
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and ids(cards) == sorted(["t", f"{SD}--{PRODUCT}--t"])
        assert len(plan.kept) == 1 and "its summary differs" in next(iter(plan.kept))

    def test_a_copy_owned_by_another_system_stays(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files, managed_by="quest-sync")])
        assert plan_and_apply(cards)[1].moved == 0


class TestExactDuplicates:
    def test_the_most_used_of_two_cards_pinning_the_same_files_stays(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("plain", files, name="a topic"), card("busy", files, usage_count=0, name="a topic")])
        busy = FilesystemCardRepository(str(cards)).read("busy")
        busy["keywords"] = ["a", "one"]       # keywords matching the files outrank ones that do not
        FilesystemCardRepository(str(cards)).write("busy", busy)
        plan_and_apply(cards)
        assert len(ids(cards)) == 1

    def test_a_used_or_learned_duplicate_is_kept_and_a_plain_one_is_not(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("plain", files, name="a topic"), card("used", files, usage_count=4, name="a topic"),
                     card("taught", files, provenance={"created_by_task": "a run"}, name="a topic")])
        plan, result = plan_and_apply(cards)
        assert ids(cards) == ["taught", "used"] and result.moved == 1
        assert moved(cards, "duplicate-files") == ["plain"]

    def test_two_protected_duplicates_are_both_kept(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("used", files, usage_count=4), card("taught", files, usage_count=1,
                                                           provenance={"created_by_task": "a run"})])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and plan.kept

    def test_two_topics_about_one_file_are_two_cards_and_both_stay(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["handbook.md"]
        seed(cards, [card("handbook-billing", files, name="handbook billing"),
                     card("handbook-auth", files, name="handbook auth")])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and ids(cards) == ["handbook-auth", "handbook-billing"]
        assert any("its name differs" in why for why in plan.kept)

    def test_a_card_that_pins_no_file_is_never_a_duplicate(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("one"), card("two")])
        assert plan_and_apply(cards)[1].moved == 0

    def test_nothing_is_ever_deleted(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files), card("dup", files)])
        before = ids(cards)
        plan_and_apply(cards)
        after = ids(cards) + moved(cards, "compounded") + moved(cards, "duplicate-files")
        assert sorted(after) == sorted(before)
        manifest = next((cards / "_quarantine").glob("*-tidy-manifest.jsonl"))
        lines = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert sum(1 for line in lines if "moved_to" in line) == len(before) - len(ids(cards))
        assert lines[-1]["summary"]["moved"] == len(before) - len(ids(cards))


class TestStoredUnderAVectorPointId:
    def test_its_notes_are_folded_into_the_card_it_stands_for_and_the_stray_is_moved(self, tmp_path):
        cards = tmp_path / "cards"
        note = {"id": "n1", "type": "note", "locator": {"text": "kept"}}
        seed(cards, [card("topic", ["a/one.md", "a/two.md"]),
                     card("card:topic", name="topic", summary="about topic", content=[note])])
        plan_and_apply(cards)
        assert ids(cards) == ["topic"]
        assert [i["id"] for i in FilesystemCardRepository(str(cards)).read("topic")["content"]] == ["n1"]
        assert moved(cards, "item-id") == ["card:topic"]

    def test_with_no_card_to_fold_into_it_becomes_that_card(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("card:orphan", content=[{"id": "n1", "type": "note", "locator": {"text": "x"}}])])
        plan_and_apply(cards)
        assert ids(cards) == ["orphan"]


NOTE = {"id": "n1", "type": "note", "locator": {"text": "taught by a run"}, "ts": 100.0}


class TestWhatAPersonAddedIsNeverMoved:
    """Review findings 2 and 3: a card somebody wrote or added to is not 'redundant' because it pins the same files."""

    def test_a_card_written_by_hand_stays_though_it_pins_the_files_of_a_topic_card(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["docs/release.md", "docs/checklist.md"]
        seed(cards, [card("release-topic", files), card("release--checklist", files, provenance={})])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and ids(cards) == ["release--checklist", "release-topic"]
        assert plan.kept == {"written by a person (no provenance)": 1}

    def test_two_cards_written_by_hand_are_both_kept(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["docs/release.md", "docs/checklist.md"]
        seed(cards, [card("one", files, provenance={}), card("two", files, provenance={})])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and plan.kept == {"written by a person (no provenance)": 1}

    def test_a_card_somebody_added_to_is_kept_whatever_its_rank_and_a_used_one_displaces_only_a_plain_one(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("busy", files, usage_count=3, name="a topic"), card("plain", files, name="a topic"),
                     card("taught", files, content=[NOTE], name="a topic"),
                     card("tagged", files, scope_tags=["quest:q1"], name="a topic")])
        plan, result = plan_and_apply(cards)
        assert ids(cards) == ["busy", "tagged", "taught"] and moved(cards, "duplicate-files") == ["plain"]

    def test_a_stray_folds_by_item_id_the_newer_item_wins_and_tags_are_the_union(self, tmp_path):
        cards = tmp_path / "cards"
        older = dict(NOTE, locator={"text": "old"}, ts=10.0)
        newer = dict(NOTE, locator={"text": "new"}, ts=20.0)
        extra = {"id": "n2", "type": "note", "locator": {"text": "only on the stray"}, "ts": 5.0}
        seed(cards, [card("topic", ["a/one.md", "a/two.md"], content=[older], scope_tags=["quest:a"]),
                     card("card:topic", name="topic", summary="about topic", content=[newer, extra],
                          scope_tags=["quest:b"])])
        plan, result = plan_and_apply(cards)
        merged = FilesystemCardRepository(str(cards)).read("topic")
        assert result.moved == 1 and moved(cards, "item-id") == ["card:topic"]
        assert {i["id"]: i["locator"]["text"] for i in merged["content"]} == {"n1": "new", "n2": "only on the stray"}
        assert merged["scope_tags"] == ["quest:a", "quest:b"]

    def test_a_stray_whose_summary_was_edited_is_left_in_place_and_reported(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("topic", ["a/one.md", "a/two.md"]),
                     card("card:topic", name="topic", summary="a better summary", content=[NOTE])])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and ids(cards) == ["card:topic", "topic"]
        assert any("summary differs" in why for why in plan.kept)

    def test_two_versions_of_a_note_with_the_same_time_cannot_be_told_apart_so_the_stray_stays(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("topic", ["a/one.md", "a/two.md"], content=[dict(NOTE, locator={"text": "one"})]),
                     card("card:topic", name="topic", summary="about topic",
                          content=[dict(NOTE, locator={"text": "another"})])])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and any("same time" in why for why in plan.kept)

    def test_a_stray_owned_by_another_system_is_left(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("quest-123"), card("card:quest-123", managed_by="quest-sync")])
        assert plan_and_apply(cards)[1].moved == 0

    def test_a_stray_that_names_a_card_of_another_store_is_left(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("topic", ["a/one.md", "a/two.md"]), card("card:st_0123456789abcdef:topic", name="topic",
                                                                  summary="about topic", content=[NOTE])])
        plan, result = plan_and_apply(cards)
        assert result.moved == 0 and any("another store" in why for why in plan.kept)

    def test_a_stray_naming_this_stores_own_qualified_id_folds_into_the_bare_card(self, tmp_path):
        cards = tmp_path / "cards"
        seed(cards, [card("topic", ["a/one.md", "a/two.md"]), card("card:st_0123456789abcdef:topic", name="topic",
                                                                  summary="about topic", content=[NOTE])])
        plan = hygiene.plan_tidy(hygiene.card_files_in(cards), own_store_id="st_0123456789abcdef")
        result = hygiene.apply_tidy(cards, plan, own_store_id="st_0123456789abcdef")
        assert result.moved == 1
        assert [i["id"] for i in FilesystemCardRepository(str(cards)).read("topic")["content"]] == ["n1"]


class TestTheMoveLooksAgain:
    """Review finding 5: a plan is a proposal; the card is read again, under the lock, just before it moves."""

    def test_a_note_added_between_the_scan_and_the_move_keeps_the_card_where_it_is(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("a-topic", files, name="a topic", provenance={"created_by_task": "bootstrap",
                                                                        "created_at": "2026-01-01T00:00:00+00:00"}),
                     card("a-topic-2", files, name="a topic", provenance={"created_by_task": "bootstrap",
                                                                          "created_at": "2026-02-01T00:00:00+00:00"})])
        plan = hygiene.plan_tidy(hygiene.card_files_in(cards))
        assert [m.card_id for m in plan.moves] == ["a-topic-2"]
        other = store(tmp_path)
        assert other.update_card("a-topic-2", add=[{"id": "n1", "type": "note", "text": "learned just now"}])
        result = hygiene.apply_tidy(cards, plan)
        assert result.moved == 0 and ids(cards) == ["a-topic", "a-topic-2"]
        assert result.left == {"has notes or edits added by a run": 1}

    def test_a_card_somebody_else_already_moved_is_not_counted_twice(self, tmp_path):
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("t", files, name="a topic"), card("dup", files, name="a topic")])
        plan = hygiene.plan_tidy(hygiene.card_files_in(cards))
        first, second = hygiene.apply_tidy(cards, plan), hygiene.apply_tidy(cards, plan)
        assert (first.moved, second.moved) == (1, 0)


def tidy_in_a_process(cards: str, root: str, barrier, results) -> None:
    logging.disable(logging.CRITICAL)
    s = FileContextStore(cards, repo_root=root, auto_bootstrap=False, mount_mode="copy", max_cards=0, self_heal=True)
    barrier.wait()
    results.put(s.tidy())


class TestSeveralProcessesTidyOneStore:
    """Review finding 8: four processes tidying one folder move every card once and leave a manifest each."""

    def test_every_file_is_kept_and_every_move_is_counted_once(self, tmp_path):
        import multiprocessing as mp
        root = tmp_path / "corpus"
        cards = root / ".quest-context"
        count = 40
        for i in range(count):
            files = [f"f{i}/a.md", f"f{i}/b.md"]
            seed(cards, [card(f"f{i}-topic", files, name=f"f{i}", provenance={"created_by_task": "bootstrap",
                                                                              "created_at": "2026-01-01T00:00:00+00:00"}),
                         card(f"f{i}-dup", files, name=f"f{i}", provenance={"created_by_task": "bootstrap",
                                                                            "created_at": "2026-02-01T00:00:00+00:00"}),
                         card(f"card:f{i}-topic", name=f"f{i}", summary=f"about f{i}-topic",
                              content=[dict(NOTE, id=f"c{i}")])])
        before = len(list(cards.glob("*.json")))
        context = mp.get_context("fork")
        barrier, results = context.Barrier(4), context.Queue()
        procs = [context.Process(target=tidy_in_a_process, args=(str(cards), str(root), barrier, results))
                 for _ in range(4)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=120)
        counted = sorted(results.get() for _ in procs)
        live = [p for p in cards.glob("*.json") if not p.name.startswith("bootstrap")]
        quarantined = list((cards / "_quarantine").glob("*/*.json"))
        assert len(live) + len(quarantined) == before, "no file was lost or made up"
        assert sum(counted) == len(quarantined) == 2 * count, "each move was counted by exactly one process"
        manifest_lines = [json.loads(line) for m in (cards / "_quarantine").glob("*-tidy-manifest.jsonl")
                          for line in m.read_text().splitlines() if "moved_to" in json.loads(line)]
        assert len(manifest_lines) == len(quarantined), "the manifests together name every move"
        for i in range(count):
            notes = [n["id"] for n in json.loads((cards / f"f{i}-topic.json").read_text())["content"]]
            assert notes == [f"c{i}"], "the note was folded once"


# --- at open -------------------------------------------------------------------------------------

class TestAtOpen:
    def inflate(self, tmp_path: Path):
        files = ["a/one.md", "a/two.md"]
        seed(tmp_path / "cards", [card("t", files)] + [card(f"{SD}--{PRODUCT}--{ROOT}--t{i}", files)
                                                      for i in range(5)])

    def test_a_store_built_to_heal_tidies_on_prewarm_and_says_so_once(self, tmp_path, caplog):
        self.inflate(tmp_path)
        said: List[str] = []
        with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
            store(tmp_path, self_heal=True).prewarm(notify=said.append)
        assert ids(tmp_path / "cards") == ["t"]
        assert said == ["Tidied 5 duplicate context cards. They are kept in a folder and can be put back."]
        text = caplog.records[-1].getMessage()
        assert text.startswith("Tidied 5 duplicate context cards. They are kept in a folder")
        assert "compounded 5" in text and "Manifest:" in text

    def test_a_store_not_built_to_heal_is_left_exactly_as_it_is(self, tmp_path):
        self.inflate(tmp_path)
        store(tmp_path).prewarm()
        assert len(ids(tmp_path / "cards")) == 6

    def test_an_indexing_pass_tidies_at_its_end(self, tmp_path):
        root = tmp_path / "corpus"
        for name in ("a", "b"):
            (root / name).mkdir(parents=True)
            for i in range(2):
                (root / name / f"n{i}.md").write_text(f"# {name}\n")
        from card_store_growth.world import DiskProvider
        s = FileContextStore(str(tmp_path / "cards"), repo_root=str(root), auto_bootstrap=False,
                             mount_mode="copy", max_cards=0, self_heal=True)
        s.bootstrap(root=str(root), provider=DiskProvider(root.resolve()))
        files = ["a/n0.md", "a/n1.md"]
        seed(tmp_path / "cards", [card(f"{SD}--{PRODUCT}--a-topic-copy", files)])
        (root / "a" / "n0.md").write_text("# a edited\n")
        s2 = FileContextStore(str(tmp_path / "cards"), repo_root=str(root), auto_bootstrap=False,
                              mount_mode="copy", max_cards=0, self_heal=True)
        s2.refresh_stale(root=str(root), provider=DiskProvider(root.resolve()))
        assert f"{SD}--{PRODUCT}--a-topic-copy" not in ids(tmp_path / "cards")

    def test_the_loader_never_holds_a_card_that_is_being_moved(self, tmp_path, monkeypatch):
        self.inflate(tmp_path)
        s = store(tmp_path, self_heal=True)
        s._load_all()                                  # the index is already built, with all six
        seen: List[List[str]] = []
        real = hygiene.apply_tidy

        def apply_while_loading(cards_dir, plan, pace, should_stop, own_store_id=""):
            seen.append(sorted(s._load_all()))         # what a turn would see in the middle of the move
            return real(cards_dir, plan, pace, should_stop, own_store_id)
        monkeypatch.setattr(hygiene, "apply_tidy", apply_while_loading)
        s.tidy()
        assert seen == [["t"]], "the cards on their way out were already left out of the loader"
        assert sorted(s._load_all()) == ["t"]

    def test_the_moves_are_paced_so_the_first_turn_is_not_starved(self, tmp_path, monkeypatch):
        self.inflate(tmp_path)
        pauses: List[int] = []
        import quest_ai_runner.adapters.context_store_heal as heal

        class CountingPacer:
            def __call__(self) -> None:
                pauses.append(1)
        monkeypatch.setattr(heal, "Pacer", CountingPacer)
        store(tmp_path, self_heal=True).tidy()
        assert len(pauses) >= 5, "the scan and every move call the pacer"


class TestAnInflatedStoreIsNotEmbedded:
    def setup_store(self, tmp_path: Path, topics: int, caplog):
        root = tmp_path / "corpus"
        for name in ("a", "b"):
            (root / name).mkdir(parents=True)
            for i in range(2):
                (root / name / f"n{i}.md").write_text(f"# {name}\n")
        seed(tmp_path / "cards", [card(f"topic-{i}", [f"f{i}/x.md", f"f{i}/y.md"]) for i in range(topics)])
        return FileContextStore(str(tmp_path / "cards"), repo_root=str(root), auto_bootstrap=False,
                                mount_mode="copy", max_cards=0, self_heal=True)

    def test_more_topic_cards_than_folders_is_an_error_and_nothing_is_exported(self, tmp_path, caplog):
        s = self.setup_store(tmp_path, topics=30, caplog=caplog)
        with caplog.at_level(logging.ERROR, logger="quest-ai-runner.context"):
            assert s.inflated() is True
            assert s.export_for_embedding() == []
        assert "30 topic cards" in caplog.text and "has only 3 folders" in caplog.text
        assert len(ids(tmp_path / "cards")) == 30, "nothing is removed for this reason"

    def test_a_store_within_its_corpus_is_embedded_as_usual(self, tmp_path, caplog):
        s = self.setup_store(tmp_path, topics=3, caplog=caplog)
        assert s.inflated() is False
        assert len(s.export_for_embedding()) == 3

    def test_with_no_per_folder_rule_the_statement_cannot_be_made(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "0")
        assert self.setup_store(tmp_path, topics=30, caplog=caplog).inflated() is False

    def test_a_store_not_built_to_heal_does_not_check(self, tmp_path, caplog):
        seed(tmp_path / "cards", [card(f"topic-{i}", [f"f{i}/x.md", f"f{i}/y.md"]) for i in range(30)])
        assert store(tmp_path).inflated() is False


# --- the account of a store -----------------------------------------------------------------------------

class TestHealth:
    def test_counts_by_kind_with_dates_and_the_folder_fact(self, tmp_path):
        root = tmp_path / "corpus"
        for name in ("a", "b"):
            (root / name).mkdir(parents=True)
            (root / name / "n.md").write_text("x")
        recent = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        copy = card("other--c", ["x/a.md", "x/b.md"],
                    provenance={"created_by_task": "bootstrap", "imported_from": "other", "created_at": recent})
        seed(tmp_path / "cards", [
            card("t1", ["a/n.md", "b/n.md"], provenance={"created_by_task": "bootstrap", "created_at": recent}),
            card("t2", ["a/n.md", "b/n.md"], provenance={"created_by_task": "bootstrap", "created_at": old}),
            card("learned", provenance={"created_by_task": "a run"}),
            card("q", managed_by="sync"), copy, card(f"{SD}--{PRODUCT}--z", ["z/a.md"]),
            card("card:strayed", provenance={"created_by_task": "a run"}),
        ])
        health = hygiene.store_health(tmp_path / "cards", root=root)
        assert (health.total, health.imported, health.own) == (7, 1, 6)
        assert (health.topic, health.learned, health.managed) == (3, 2, 1)
        assert health.compounded_ids == 1 and health.item_id_cards == 1
        assert health.duplicate_file_sets == 1
        assert (health.dated, health.recent) == (3, 2)
        assert health.folders == 3            # ".", "a" and "b" hold or contain indexable files
        table = health.render()
        assert "cards in the index" in table and "created in the last 14 days" in table

    def test_the_dry_run_of_the_script_prints_the_table_by_reason_with_dates(self, tmp_path, capsys):
        spec = importlib.util.spec_from_file_location(
            "quarantine_cards_tool", Path(__file__).resolve().parent.parent / "scripts" / "quarantine_cards.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        files = ["a/one.md", "a/two.md"]
        recent = datetime.now(timezone.utc).isoformat()
        seed(tmp_path / "cards", [
            card("t", files),
            card(f"{SD}--{PRODUCT}--t", files, provenance={"created_by_task": "bootstrap", "created_at": recent}),
            card(f"{SD}--{PRODUCT}--{ROOT}--t", files)])
        assert tool.main(["--cards-dir", str(tmp_path / "cards")]) == 0
        out = capsys.readouterr().out
        assert "store health" in out and "would move, by reason" in out
        table = out.split("would move, by reason")[1]
        assert "compounded" in table and "1 of 1 dated" in table
        assert len(ids(tmp_path / "cards")) == 3, "a dry run moves nothing"


class TestTheTidyCommand:
    """``quest-ai-runner cards tidy``: the installed package's way to do what the cleanup script does for provable copies."""

    def test_it_is_a_dry_run_unless_asked(self, tmp_path, capsys):
        from quest_ai_runner.cli import main
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files), card("mine", files, provenance={})])
        assert main(["cards", "tidy", "--cards-dir", str(cards)]) == 0
        out = capsys.readouterr().out
        assert "would move      : 1" in out and "written by a person" in out and "dry run" in out
        assert len(ids(cards)) == 3

    def test_apply_moves_the_copy_and_never_the_card_a_person_wrote(self, tmp_path, capsys):
        from quest_ai_runner.cli import main
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("t", files), card(f"{SD}--{PRODUCT}--t", files), card("mine", files, provenance={})])
        assert main(["cards", "tidy", "--cards-dir", str(cards), "--apply"]) == 0
        assert ids(cards) == ["mine", "t"] and moved(cards, "compounded") == [f"{SD}--{PRODUCT}--t"]
        assert "manifest" in capsys.readouterr().out


def test_the_limit_and_the_tidy_agree_on_what_a_topic_card_is():
    assert card_limit.card_kind(card("t", ["a/one.md"])) == card_limit.KIND_TOPIC
    assert card_limit.card_kind(card("t", managed_by="x")) == card_limit.KIND_MANAGED
    assert card_limit.card_kind(card("t", provenance={"created_by_task": "bootstrap",
                                                      "origin_store": "st_x"})) == card_limit.KIND_IMPORTED
