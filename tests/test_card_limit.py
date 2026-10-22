"""The card limit: 1,000 cards by default, and it never acts without saying so.

A card quota was tried and removed on 2026-10-05 because it silently refused to index and left a person to
guess the number. The limit now is the owner's number (``QAR_MAX_CARDS``), and what these tests pin is the
other half of it: at the limit a new derived card is refused AND counted AND announced with what fills the
store and how to change the limit, a card learned from a run or written for a quest is still admitted by
moving the coldest derived card out (never deleting one), and a store that is already over the limit is told
so and left alone.

Everything is offline: a fake model, tmp folders.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from quest_ai_runner.adapters import card_limit
from quest_ai_runner.adapters.card_limit_report import REPORT_NAME, breakdown, notice_lines, report_for
from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
from card_store_growth.world import DiskProvider  # noqa: E402

LOGGER = "quest-ai-runner.card-limit"


@pytest.fixture(autouse=True)
def fresh_announcements(monkeypatch):
    monkeypatch.delenv("QAR_MAX_CARDS", raising=False)
    card_limit.reset_announcements()
    yield
    card_limit.reset_announcements()


def corpus(root: Path, folders: List[str]) -> Path:
    for name in folders:
        (root / name).mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (root / name / f"note{i}.md").write_text(f"# {name} {i}\nabout {name}\n")
    return root


def store_for(tmp_path: Path, root: Path, **kwargs: Any) -> FileContextStore:
    return FileContextStore(str(tmp_path / "cards"), repo_root=str(root), auto_bootstrap=False,
                            mount_mode="copy", **kwargs)


def index(store: FileContextStore, root: Path, refresh: bool = False) -> int:
    call = store.refresh_stale if refresh else store.bootstrap
    return call(root=str(root), provider=DiskProvider(root.resolve()))


def card(card_id: str, *, files: List[str] = (), kind: str = "topic", **extra: Any) -> Dict[str, Any]:
    provenance: Dict[str, Any] = {"created_by_task": "bootstrap" if kind in ("topic", "imported") else "a run"}
    if kind == "imported":
        provenance.update({"imported_from": "other", "origin_store": "st_other", "origin_id": card_id,
                           "origin_root": "other"})
    out = {"id": card_id, "name": card_id, "summary": f"about {card_id}", "keywords": [card_id],
           "files": [{"path": p} for p in files], "usage_count": 0, "provenance": provenance}
    if kind == "managed":
        out["managed_by"] = "quest-sync"
    out.update(extra)
    return out


def seed(cards_dir: Path, cards: List[Dict[str, Any]]) -> None:
    repo = FilesystemCardRepository(str(cards_dir))
    for c in cards:
        assert repo.write(c["id"], c)


def card_ids(cards_dir: Path) -> List[str]:
    return sorted(FilesystemCardRepository(str(cards_dir)).load_all())


# --- the knob -----------------------------------------------------------------------------------

class TestTheKnob:
    def test_the_default_is_a_thousand(self):
        assert card_limit.max_cards_from_env({}) == 1000

    @pytest.mark.parametrize("raw,expected", [("2500", 2500), ("2,500", 2500), (" 40 ", 40), ("0", 0),
                                              ("unlimited", 0), ("None", 0), ("off", 0)])
    def test_the_environment_sets_it_and_zero_or_a_word_means_none(self, raw, expected):
        assert card_limit.max_cards_from_env({"QAR_MAX_CARDS": raw}) == expected

    def test_a_value_that_is_not_a_number_falls_back_loudly(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert card_limit.max_cards_from_env({"QAR_MAX_CARDS": "lots"}) == 1000
        assert "QAR_MAX_CARDS" in caplog.text and "0 for no limit" in caplog.text

    def test_a_store_takes_its_limit_from_the_environment_unless_told(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QAR_MAX_CARDS", "77")
        assert store_for(tmp_path, tmp_path).limit_guard.limit == 77
        assert store_for(tmp_path, tmp_path, max_cards=5).limit_guard.limit == 5
        assert store_for(tmp_path, tmp_path, max_cards=0).limit_guard.enabled is False

    def test_a_shared_backend_is_not_limited_by_a_local_number(self, tmp_path):
        backend = FilesystemCardRepository(str(tmp_path / "elsewhere"))
        store = FileContextStore(str(tmp_path / "cards"), repo_root=str(tmp_path), auto_bootstrap=False,
                                 card_repository=backend, max_cards=3)
        assert store.limit_guard.enabled is False


# --- at the limit ---------------------------------------------------------------------------------

class TestAtTheLimit:
    def test_a_new_derived_card_is_refused_and_counted(self, tmp_path):
        root = corpus(tmp_path / "corpus", ["a", "b", "c", "d", "e"])
        store = store_for(tmp_path, root, max_cards=3)
        index(store, root)
        assert len(card_ids(tmp_path / "cards")) == 3
        assert store.limit_guard.state.waiting == 2
        assert len(store.limit_guard.refused_ids) == 2

    def test_a_card_that_already_exists_is_refreshed_in_place(self, tmp_path):
        root = corpus(tmp_path / "corpus", ["a", "b"])
        store = store_for(tmp_path, root, max_cards=2)
        index(store, root)
        before = FilesystemCardRepository(str(tmp_path / "cards")).load_all()
        (root / "a" / "note0.md").write_text("# a\nchanged words\n")
        store = store_for(tmp_path, root, max_cards=2)
        index(store, root, refresh=True)
        after = FilesystemCardRepository(str(tmp_path / "cards")).load_all()
        assert sorted(after) == sorted(before) and len(after) == 2
        old = {f["path"]: f["sha256"] for f in before["a-topic"]["files"]}
        new = {f["path"]: f["sha256"] for f in after["a-topic"]["files"]}
        assert old["a/note0.md"] != new["a/note0.md"], "the card was refreshed although the store is full"

    def test_a_full_store_does_not_ask_the_model_for_cards_it_would_refuse(self, tmp_path):
        root = corpus(tmp_path / "corpus", ["a", "b"])
        store = store_for(tmp_path, root, max_cards=2)
        index(store, root)
        corpus(root, ["c", "d"])
        provider = MagicMock()
        provider.answer.return_value = "[]"
        provider.list_models.return_value = []
        store_for(tmp_path, root, max_cards=2).refresh_stale(root=str(root), provider=provider)
        assert provider.answer.call_count == 0

    def test_the_files_left_waiting_are_indexed_once_the_limit_is_raised(self, tmp_path):
        # The failure of 2026-10-05: a refused folder was recorded as "seen", so raising the limit
        # never picked it up.
        root = corpus(tmp_path / "corpus", ["a", "b", "c"])
        index(store_for(tmp_path, root, max_cards=2), root)
        assert len(card_ids(tmp_path / "cards")) == 2
        index(store_for(tmp_path, root, max_cards=0), root, refresh=True)
        assert len(card_ids(tmp_path / "cards")) == 3

    def test_no_limit_means_no_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QAR_MAX_CARDS", "0")
        root = corpus(tmp_path / "corpus", [f"f{i}" for i in range(12)])
        index(store_for(tmp_path, root), root)
        assert len(card_ids(tmp_path / "cards")) == 12


class TestLearnedAndManagedCards:
    def full_store(self, tmp_path: Path, limit: int = 4):
        cards = [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(limit)]
        seed(tmp_path / "cards", cards)
        return store_for(tmp_path, tmp_path, max_cards=limit)

    def test_a_learned_card_moves_the_coldest_derived_card_out_and_nothing_is_deleted(self, tmp_path):
        store = self.full_store(tmp_path)
        repo = FilesystemCardRepository(str(tmp_path / "cards"))
        for cid in ("topic-0", "topic-1", "topic-2"):                 # used cards are not the coldest
            used = repo.read(cid)
            used["usage_count"] = 2
            repo.write(cid, used)
        store.record("fix the chat bug", {"kind": "met", "files": []})
        ids = card_ids(tmp_path / "cards")
        assert len(ids) == 4, "the total stays at the limit"
        assert "topic-3" not in ids and "topic-0" in ids
        moved = tmp_path / "cards" / "_quarantine" / "limit"
        assert (moved / "topic-3.json").exists(), "moved, not deleted"
        line = json.loads((moved / "manifest.jsonl").read_text().splitlines()[0])
        assert line["moved"] == "topic-3" and line["kind"] == "topic" and "limit" in line["why"]
        assert store.limit_guard.state.evicted == 1

    def test_a_card_written_for_a_quest_is_admitted_the_same_way(self, tmp_path):
        store = self.full_store(tmp_path)
        assert store.write_card("quest-1", card("quest-1", kind="managed"))
        ids = card_ids(tmp_path / "cards")
        assert "quest-1" in ids and len(ids) == 4
        assert len(list((tmp_path / "cards" / "_quarantine" / "limit").glob("*.json"))) == 1

    def test_a_store_of_only_learned_cards_admits_the_card_and_says_so(self, tmp_path, caplog):
        store = store_for(tmp_path, tmp_path, max_cards=2)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            for text in ("fix the chat bug", "add billing export", "explain search"):
                store.record(text, {"kind": "met", "files": []})
        assert len(card_ids(tmp_path / "cards")) == 3
        assert store.limit_guard.state.admitted_over == 1
        assert not (tmp_path / "cards" / "_quarantine").exists()
        assert "1 learned cards were kept above the limit" in caplog.text

    def test_an_edit_to_a_card_that_exists_is_not_a_new_card(self, tmp_path):
        store = self.full_store(tmp_path)
        assert store.add_content("topic-0", {"type": "note", "locator": {"text": "learned"}, "why": "w"})
        assert len(card_ids(tmp_path / "cards")) == 4
        assert not (tmp_path / "cards" / "_quarantine").exists()


# --- saying so --------------------------------------------------------------------------------------

def mixed_store(tmp_path: Path):
    """10 cards: topics in alpha (2), beta, gamma; 2 imported; 3 learned; 1 from a quest."""
    seed(tmp_path / "cards", [
        card("alpha-1", files=["alpha/a.md", "alpha/b.md"]), card("alpha-2", files=["alpha/sub/a.md", "alpha/sub/b.md"]),
        card("beta-1", files=["beta/a.md", "beta/b.md"]), card("gamma-1", files=["gamma/a.md", "gamma/b.md"]),
        card("other--one", files=["x/a.md", "x/b.md"], kind="imported"),
        card("other--two", files=["y/a.md", "y/b.md"], kind="imported"),
        card("learned-1", kind="learned"), card("learned-2", kind="learned"), card("learned-3", kind="learned"),
        card("quest-1", kind="managed"),
    ])
    return store_for(tmp_path, tmp_path, max_cards=10)


class TestTheNotice:
    def refuse_one(self, store: FileContextStore) -> bool:
        return store._store_write("new-topic", card("new-topic", files=["new/a.md", "new/b.md"]))

    def test_it_says_what_fills_the_store_in_three_short_lines(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert self.refuse_one(store) is False
        shown = [r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True)]
        assert shown == [
            "Your context store is full at 10 cards, so new topic cards are not being added.",
            "It holds 4 topic cards (the biggest folders: alpha 2, beta 1, gamma 1), 2 imported from other "
            "stores, 3 learned from runs, 1 from quests.",
            "To allow more, set QAR_MAX_CARDS to a bigger number, or ask the AI: raise the QAR card limit to 2000.",
        ]
        assert all("—" not in line for line in shown)

    def test_the_log_carries_the_instructions_an_ai_needs_to_change_it(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
        full = next(r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True) is False)
        for needed in ("QAR_MAX_CARDS", "0 (or the word unlimited) means no limit", "env_files", "[env]",
                       "Environment=", "restart", str(tmp_path / "cards" / REPORT_NAME),
                       "quest-ai-runner cards report", "_quarantine"):
            assert needed in full, needed

    def test_it_fires_once_per_process(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
            first = len(caplog.records)
            self.refuse_one(store)
            store._store_write("another", card("another", files=["z/a.md", "z/b.md"]))
        assert first == 4 and len(caplog.records) == 4

    def test_the_report_file_has_the_breakdown_and_the_ways_to_change_the_limit(self, tmp_path):
        store = mixed_store(tmp_path)
        self.refuse_one(store)
        text = (tmp_path / "cards" / REPORT_NAME).read_text()
        assert "- Limit: 10 cards (QAR_MAX_CARDS)" in text
        assert "- Cards in the index: 10" in text
        assert "| Topic cards (written from the corpus) | 4 |" in text
        assert "| alpha | 2 |" in text and "| beta | 1 |" in text
        assert "| other | 2 |" in text, "imported cards are counted by their source store"
        assert "| Learned from runs | 3 |" in text and "| From quests (managed) | 1 |" in text
        for needed in ("QAR_MAX_CARDS", "unlimited", "env_files", "[env]", "_quarantine", "Coldest derived cards"):
            assert needed in text

    def test_the_report_counts_what_it_refused_and_moved(self, tmp_path):
        store = mixed_store(tmp_path)
        store.limit_guard.begin_pass()
        self.refuse_one(store)
        store.limit_guard.end_pass()
        store.record("fix the chat bug again", {"kind": "met", "files": []})
        report = report_for(str(tmp_path / "cards"), 10)
        assert "waiting for room (latest pass): 1" in report
        assert "moved out to make room for a learned or quest card: 1" in report
        assert "| limit | 1 |" in report, "the moved card is listed by reason"

    def test_cards_report_prints_it(self, tmp_path, capsys):
        from quest_ai_runner.cli import main
        mixed_store(tmp_path)
        assert main(["cards", "report", "--cards-dir", str(tmp_path / "cards")]) == 0
        out = capsys.readouterr().out
        assert "# Card limit report" in out and "| Topic cards (written from the corpus) | 4 |" in out

    def test_a_store_over_the_limit_at_open_says_so_and_deletes_nothing(self, tmp_path, caplog):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(12)])
        store = store_for(tmp_path, tmp_path, max_cards=5)
        before = card_ids(tmp_path / "cards")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            store.prewarm()
        shown = [r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True)]
        assert shown[0] == ("Your context store holds 12 cards, over its limit of 5, so new topic cards "
                            "are not being added.")
        assert card_ids(tmp_path / "cards") == before
        assert not (tmp_path / "cards" / "_quarantine").exists()

    def test_a_store_under_its_limit_says_nothing(self, tmp_path, caplog):
        seed(tmp_path / "cards", [card("one", files=["a/a.md", "a/b.md"])])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            store_for(tmp_path, tmp_path, max_cards=5).prewarm()
        assert caplog.records == []

    def test_the_breakdown_names_nothing_that_is_not_in_the_store(self, tmp_path):
        mixed_store(tmp_path)
        cards = FilesystemCardRepository(str(tmp_path / "cards")).load_all()
        b = breakdown(cards, tmp_path / "cards", 10)
        assert (b.topic, b.imported, b.learned, b.managed, b.total) == (4, 2, 3, 1, 10)
        assert notice_lines(b)[0].startswith("Your context store is full at 10 cards")


# --- the other writers -------------------------------------------------------------------------------

def test_a_merge_that_would_pass_the_limit_is_a_problem_and_applies_nothing(tmp_path):
    from quest_ai_runner.adapters import card_store_identity as ident
    from quest_ai_runner.adapters.card_store_merge import apply_merge
    wide, narrow = tmp_path / "wide" / ".quest-context", tmp_path / "wide" / "sub" / ".quest-context"
    seed(wide, [card(f"w{i}", files=[f"w{i}/a.md", f"w{i}/b.md"]) for i in range(3)])
    seed(narrow, [card(f"n{i}", files=[f"n{i}/a.md", f"n{i}/b.md"]) for i in range(3)])
    for directory, root in ((wide, wide.parent), (narrow, narrow.parent)):
        ident.write_meta(str(directory), {"version": 4, "card_count": 3, "root": str(root)})
        ident.ensure_store_id(str(directory))
    plan = apply_merge(str(narrow), str(wide), prune_source_after=True, max_cards=5)
    assert plan.problems and "over its limit of 5" in plan.problems[0]
    assert not plan.applied and len(card_ids(wide)) == 3 and len(card_ids(narrow)) == 3
    assert apply_merge(str(narrow), str(wide), prune_source_after=True, max_cards=6).applied
