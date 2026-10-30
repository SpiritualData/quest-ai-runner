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

from hubofhubs.recall import card_limit
from hubofhubs.recall.card_limit_report import REPORT_NAME, breakdown
from quest_ai_runner.adapters import card_store_settings
from quest_ai_runner.adapters.card_limit_report import VOICE, notice_lines, report_for
from hubofhubs.recall.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
from card_store_growth.world import DiskProvider  # noqa: E402

LOGGER = "hubofhubs.recall.card_limit"


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
        assert card_store_settings.max_cards_from_env({}) == 1000

    @pytest.mark.parametrize("raw,expected", [("2500", 2500), ("2,500", 2500), (" 40 ", 40), ("0", 0),
                                              ("unlimited", 0), ("None", 0), ("off", 0)])
    def test_the_environment_sets_it_and_zero_or_a_word_means_none(self, raw, expected):
        assert card_store_settings.max_cards_from_env({"QAR_MAX_CARDS": raw}) == expected

    def test_a_value_that_is_not_a_number_falls_back_loudly(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert card_store_settings.max_cards_from_env({"QAR_MAX_CARDS": "lots"}) == 1000
        assert "QAR_MAX_CARDS" in caplog.text and "0 for no limit" in caplog.text

    def test_the_library_applies_no_limit_unless_its_caller_passes_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QAR_MAX_CARDS", "77")
        assert store_for(tmp_path, tmp_path).limit_guard.enabled is False, "a library caller never gets a number it did not choose"
        assert store_for(tmp_path, tmp_path, max_cards=5).limit_guard.limit == 5
        assert store_for(tmp_path, tmp_path, max_cards=0).limit_guard.enabled is False

    @pytest.mark.parametrize("env,expected", [(None, 1000), ("77", 77), ("0", 0), ("unlimited", 0)])
    def test_the_runner_builds_its_store_with_the_limit_from_the_environment(self, tmp_path, monkeypatch, env, expected):
        """The 1,000 default belongs to the runner (its builder), not to the library under it."""
        from unittest.mock import MagicMock
        import quest_ai_runner.adapters as adapters_mod
        import quest_ai_runner.adapters.qdrant_vector_store as qdrant_module
        from quest_ai_runner.config import RunnerConfig, resolve_context_assembler
        for name in ("QAR_QUEST_API_URL", "QAR_QUEST_API_KEY", "QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID",
                     "QAR_QDRANT_URL", "QAR_VECTOR_QDRANT_URL", "QDRANT_URL", "QAR_EMBEDDER_BACKEND",
                     "QAR_VECTOR_BACKEND", "QAR_CARD_MOUNT", "QAR_MAX_CARDS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(adapters_mod, "QdrantVectorStore", MagicMock(name="QdrantVectorStore"))
        monkeypatch.setattr(qdrant_module, "make_voyage_embedder", MagicMock(name="make_voyage_embedder"))
        monkeypatch.setenv("QAR_CARDS_BACKEND", "local")
        if env is not None:
            monkeypatch.setenv("QAR_MAX_CARDS", env)
        monkeypatch.setattr("quest_ai_runner.config_assembler._bootstrap_if_needed", lambda *a, **k: None)
        monkeypatch.setattr(FileContextStore, "prewarm", lambda self, notify=None: 0)
        root = tmp_path / "repo"
        root.mkdir()
        cfg = RunnerConfig(retrieval=None, model_provider=MagicMock(), corpus_root=str(root),
                           context_cards_dir=str(tmp_path / "cards"))
        assembler = resolve_context_assembler(cfg)
        pending = [assembler]
        found = None
        while pending and found is None:
            node = pending.pop()
            if isinstance(node, FileContextStore):
                found = node
            pending.extend(getattr(node, "assemblers", None) or [])
            pending.extend(n for n in (getattr(node, attr, None) for attr in ("_keyword", "keyword")) if n is not None)
        assert found is not None and found.limit_guard.limit == expected
        assert found.heal_enabled is True, "the runner's store tidies provable copies at open"

    def test_a_shared_backend_is_limited_too(self, tmp_path):
        # The limit counts the store's own cards on every repository (a backend held 5,488 with no notice).
        backend = FilesystemCardRepository(str(tmp_path / "elsewhere"))
        store = FileContextStore(str(tmp_path / "cards"), repo_root=str(tmp_path), auto_bootstrap=False,
                                 card_repository=backend, max_cards=3)
        assert store.limit_guard.enabled is True and store.limit_guard.limit == 3


# --- at the limit ---------------------------------------------------------------------------------

class TestAtTheLimit:
    def test_a_new_derived_card_is_refused_and_counted(self, tmp_path):
        root = corpus(tmp_path / "corpus", ["a", "b", "c", "d", "e"])
        store = store_for(tmp_path, root, max_cards=3)
        index(store, root)
        assert len(card_ids(tmp_path / "cards")) == 3
        assert store.limit_guard.state.unindexed_files == 4, "two folders of two files wait for room"
        assert not store.limit_guard.refused_ids, "and no model call was spent on cards that would be refused"

    def test_the_notice_describes_the_store_as_it_is_after_the_pass_not_before(self, tmp_path, caplog):
        root = corpus(tmp_path / "corpus", ["a", "b", "c", "d", "e"])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            index(store_for(tmp_path, root, max_cards=3), root)
        first = next(r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True))
        assert first == "Context store full at 3: 3 topic, 0 imported, 0 learned, 0 quest"

    def test_a_fresh_bootstrap_asks_the_model_about_as_many_folders_as_there_is_room_for_newest_first(self, tmp_path):
        """Review finding 11: 30 folders at a limit of 10 cost 31 calls over 80 files, kept an arbitrary 10
        (the alphabetically first, archives included) and paid the same again at every refresh."""
        import os
        folders = [f"{kind}-{i:02d}" for kind in ("archive", "product") for i in range(8)]
        root = corpus(tmp_path / "corpus", folders)
        for number, name in enumerate(folders):                         # archives are the oldest, products the newest
            for note in (root / name).glob("*.md"):
                os.utime(note, (1_700_000_000 + number * 100, 1_700_000_000 + number * 100))
        sent: List[str] = []

        class Counting:
            calls = 0

            def list_models(self):
                return []

            def answer(self, messages, model=None, **kwargs):
                Counting.calls += 1
                text = json.dumps(messages)
                topics = []
                for name in folders:
                    files = [f"{name}/note{i}.md" for i in range(2) if f"{name}/note{i}.md" in text]
                    if files:
                        sent.extend(files)
                        topics.append({"id": f"{name}-topic", "name": name, "keywords": [name, "x"],
                                       "summary": f"Covers {name}.", "files": files})
                return json.dumps(topics)

        store = store_for(tmp_path, root, max_cards=6)
        written = store.bootstrap(root=str(root), provider=Counting())
        kept = card_ids(tmp_path / "cards")
        assert written == 6 and len(kept) == 6
        assert kept == sorted(f"product-{i:02d}-topic" for i in range(2, 8)), "the most recently changed folders"
        assert {rel.split("/")[0] for rel in sent} <= {f"product-{i:02d}" for i in range(2, 8)}, "only they were sent"
        assert not store.limit_guard.refused_ids
        assert store.limit_guard.state.unindexed_files == 20, "the other ten folders wait, they are not 'seen'"
        calls = Counting.calls
        store_for(tmp_path, root, max_cards=6).refresh_stale(root=str(root), provider=Counting())
        assert Counting.calls == calls, "a full store does not pay again at the next refresh"

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

    def test_learned_cards_in_a_row_each_move_one_cold_card_and_the_total_stays_at_the_limit(self, tmp_path):
        """The notice rebuilt the snapshot in the middle of an admission, so the card being admitted was forgotten
        and every second learned card went in without making room (a store of 4 held 5 after two)."""
        store = self.full_store(tmp_path)
        for text in ("fix the chat bug", "add billing export", "explain search"):
            store.record(text, {"kind": "met", "files": []})
            assert len(card_ids(tmp_path / "cards")) == 4
        assert len(list((tmp_path / "cards" / "_quarantine" / "limit").glob("*.json"))) == 3

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
        assert "1 learned or quest cards were kept above the limit" in caplog.text

    def test_an_edit_to_a_card_that_exists_is_not_a_new_card(self, tmp_path):
        store = self.full_store(tmp_path)
        assert store.add_content("topic-0", {"type": "note", "locator": {"text": "learned"}, "why": "w"})
        assert len(card_ids(tmp_path / "cards")) == 4
        assert not (tmp_path / "cards" / "_quarantine").exists()


NOTE = {"id": "n1", "type": "note", "locator": {"text": "learned by a run"}, "ts": 1.0}


class TestTheLimitNeverMovesWhatSomebodyAdded:
    """Review finding 4: a topic card with notes is not 'cold' because nobody counted a use."""

    @pytest.mark.parametrize("extra", [
        {"content": [NOTE]}, {"scope_tags": ["quest:q1"]}, {"usage_count": 2}, {"conventions": ["use tabs"]},
        {"managed_by": "quest-sync"}, {"provenance": {"created_by_task": "a run"}}, {"provenance": {}},
    ], ids=["notes", "quest tags", "used", "conventions", "owned", "learned provenance", "written by hand"])
    def test_such_a_card_is_never_the_one_that_moves(self, tmp_path, extra):
        cards = [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(3)]
        cards[0].update(extra)
        seed(tmp_path / "cards", cards)
        store = store_for(tmp_path, tmp_path, max_cards=3)
        assert store.write_card("quest-1", card("quest-1", kind="managed"))
        assert "topic-0" in card_ids(tmp_path / "cards")
        moved = sorted(p.stem for p in (tmp_path / "cards" / "_quarantine" / "limit").glob("*.json"))
        assert len(moved) == 1 and moved[0] != "topic-0"

    def test_when_every_derived_card_holds_something_the_new_card_is_admitted_and_it_is_said(self, tmp_path, caplog):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"], content=[NOTE])
                                  for i in range(3)])
        store = store_for(tmp_path, tmp_path, max_cards=3)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert store.write_card("quest-1", card("quest-1", kind="managed"))
        assert len(card_ids(tmp_path / "cards")) == 4
        assert not (tmp_path / "cards" / "_quarantine").exists()
        assert store.limit_guard.state.admitted_over == 1

    def test_a_card_that_gained_a_note_since_the_snapshot_is_not_moved(self, tmp_path):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(3)])
        store = store_for(tmp_path, tmp_path, max_cards=3)
        store.limit_guard.snapshot()                                   # the store has looked at all three
        other = store_for(tmp_path, tmp_path, max_cards=0)             # another process teaches the coldest one
        coldest = min(store.limit_guard.snapshot(), key=lambda cid: card_limit.coldness(cid, store.limit_guard.snapshot()[cid]))
        assert other.add_content(coldest, {"type": "note", "locator": {"text": "just learned"}, "why": "w"})
        assert store.write_card("quest-1", card("quest-1", kind="managed"))
        assert coldest in card_ids(tmp_path / "cards")


class TestAStaleWriterDoesNotUndoTheLimit:
    """Review findings 6 and (round 2) 5: an updater that was shown a card another process has since moved
    wrote an empty stub under its id and each stub evicted one more card; refusing it instead dropped the note."""

    def test_a_note_for_a_card_the_limit_moved_out_brings_the_card_back_with_the_note(self, tmp_path, caplog):
        seed(tmp_path / "cards", [card(f"{n}-topic", files=[f"{n}/a.md", f"{n}/b.md"]) for n in "abc"])
        stale = store_for(tmp_path, tmp_path, max_cards=3)
        stale._load_all()
        busy = store_for(tmp_path, tmp_path, max_cards=3)
        assert busy.update_card("lesson-1", add=[{"id": "l1", "text": "lesson"}])
        moved = [p.stem for p in (tmp_path / "cards" / "_quarantine" / "limit").glob("*.json")]
        assert len(moved) == 1
        with caplog.at_level(logging.INFO, logger="quest-ai-runner.context"):
            assert stale.update_card(moved[0], add=[{"id": "n1", "text": "late note"}]) is True
        back = FilesystemCardRepository(str(tmp_path / "cards")).read(moved[0])
        assert back["summary"] == f"about {moved[0]}", "the card as it was, not an empty stub"
        assert [i["id"] for i in back["content"]] == ["n1"]
        assert len(card_ids(tmp_path / "cards")) == 4, "kept over the limit, and nobody else was moved"
        assert stale.limit_guard.state.admitted_over == 1
        assert "is back in the index" in caplog.text

    def test_a_note_for_a_copy_that_was_moved_out_lands_on_the_card_that_stands_for_it(self, tmp_path, caplog):
        from hubofhubs.recall import card_store_hygiene as hygiene
        cards = tmp_path / "cards"
        files = ["a/one.md", "a/two.md"]
        seed(cards, [card("topic", files=files, name="a topic"),
                     card("sd-aaaaaa--product-bbbbbb--topic", files=files, name="a topic")])
        stale = store_for(tmp_path, tmp_path, max_cards=0)
        stale._load_all()
        hygiene.apply_tidy(cards, hygiene.plan_tidy(hygiene.card_files_in(cards)))
        assert card_ids(cards) == ["topic"]
        with caplog.at_level(logging.INFO, logger="quest-ai-runner.context"):
            assert stale.add_content("sd-aaaaaa--product-bbbbbb--topic", {"id": "n1", "type": "note",
                                                                         "locator": {"text": "late note"}, "why": "w"})
        assert card_ids(cards) == ["topic"], "no stub was created"
        assert [i["id"] for i in FilesystemCardRepository(str(cards)).read("topic")["content"]] == ["n1"]
        assert "stands for it now" in caplog.text

    def test_a_note_that_can_land_nowhere_is_logged_in_full_never_dropped_quietly(self, tmp_path, caplog):
        cards = tmp_path / "cards"
        seed(cards, [card("topic", files=["a/one.md", "a/two.md"])])
        (cards / "_quarantine" / "by-hand").mkdir(parents=True)
        (cards / "topic.json").rename(cards / "_quarantine" / "by-hand" / "topic.json")
        with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
            assert store_for(tmp_path, tmp_path, max_cards=0).add_content(
                "topic", {"id": "n1", "type": "note", "locator": {"text": "late note"}, "why": "w"}) is False
        assert card_ids(cards) == []
        assert "card update NOT applied" in caplog.text and "late note" in caplog.text


@pytest.fixture
def lock_log(monkeypatch):
    """Every acquisition of a card folder's lock in this process: whether it waited, and what the folder's
    repository was asked to do while it was held (``load_all`` parses every card, ``read`` one)."""
    from hubofhubs.recall.card_store_lock import StoreLock
    log = {"acquired": [], "held": 0, "under_lock": []}
    real_acquire, real_release = StoreLock.acquire, StoreLock.release

    def acquire(self, blocking=True):
        if log["held"] == 0:
            log["acquired"].append(blocking)          # only a first taking can wait; taking it again never does
        got = real_acquire(self, blocking)
        if got:
            log["held"] += 1
        return got

    def release(self):
        log["held"] -= 1
        return real_release(self)
    monkeypatch.setattr(StoreLock, "acquire", acquire)
    monkeypatch.setattr(StoreLock, "release", release)
    for name in ("load_all", "read"):
        real = getattr(FilesystemCardRepository, name)

        def wrapped(self, *args, _real=real, _name=name, **kwargs):
            if log["held"] > 0:
                log["under_lock"].append(_name)
            return _real(self, *args, **kwargs)
        monkeypatch.setattr(FilesystemCardRepository, name, wrapped)
    return log


class TestNothingOnATurnWaitsForTheFolderLock:
    """Round 2 finding 1: the usage stamp in assemble() took the cross-process lock by waiting, so a turn waited as
    long as another process held it (5 s measured)."""

    def assembling_store(self, tmp_path):
        root = corpus(tmp_path / "corpus", ["alpha", "beta", "gamma"])
        index(store_for(tmp_path, root, max_cards=0), root)
        return store_for(tmp_path, root, max_cards=0, confidence_threshold=0.0), root

    def test_assembly_stamps_usage_without_ever_waiting_for_the_lock(self, tmp_path, lock_log):
        store, _ = self.assembling_store(tmp_path)
        store._load_all()
        lock_log["acquired"].clear()
        assembled = store.assemble("tell me about alpha")
        assert assembled.card_ids, "the turn used a card"
        assert lock_log["acquired"] and True not in lock_log["acquired"], "every attempt was without waiting"
        used = FilesystemCardRepository(str(tmp_path / "cards")).read(assembled.card_ids[0])
        assert any(fe.get("use_count") for fe in used["files"]), "and the stamp was written when the lock was free"

    def test_a_turn_while_another_thread_holds_the_lock_skips_the_stamp_and_goes_on(self, tmp_path):
        import threading
        store, _ = self.assembling_store(tmp_path)
        store._load_all()
        held, release = threading.Event(), threading.Event()

        def hold():
            with store.folder_lock():
                held.set()
                release.wait(30)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(10)
        try:
            assembled = store.assemble("tell me about alpha")      # would never return if it waited
        finally:
            release.set()
            holder.join(10)
        assert assembled.card_ids
        used = FilesystemCardRepository(str(tmp_path / "cards")).read(assembled.card_ids[0])
        assert not any(fe.get("use_count") for fe in used["files"]), "nothing was stamped: the lock was busy"


class TestTheLimitDoesNoStoreSizedWorkUnderTheLock:
    """Round 2 finding 2: the limit rebuilt a snapshot of every card and ranked them all while holding the
    lock every other process needs, so one learned write cost 0.9 to 6.9 s at 140,000 cards."""

    def test_a_learned_write_to_a_store_above_its_limit_parses_nothing_under_the_lock(self, tmp_path, lock_log):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(60)])
        store = store_for(tmp_path, tmp_path, max_cards=20)
        store._load_all()
        other = store_for(tmp_path, tmp_path, max_cards=20)
        with other.limit_guard.folder:
            other.limit_guard.folder.bump()                          # another process changed the folder
        lock_log["under_lock"].clear()
        assert store.update_card("lesson-1", add=[{"id": "l1", "type": "note", "locator": {"text": "x"}, "why": "w"}])
        assert "load_all" not in lock_log["under_lock"]
        assert lock_log["under_lock"].count("read") <= 2, "one read of the card being edited, no more"

    def test_a_learned_write_at_the_limit_chooses_off_the_lock_and_reads_only_the_card_it_moves(self, tmp_path, lock_log):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(20)])
        store = store_for(tmp_path, tmp_path, max_cards=20)
        store._load_all()
        lock_log["under_lock"].clear()
        assert store.update_card("lesson-1", add=[{"id": "l1", "type": "note", "locator": {"text": "x"}, "why": "w"}])
        assert "load_all" not in lock_log["under_lock"]
        assert lock_log["under_lock"].count("read") <= 3, "the card being edited and the one that moves"
        assert len(list((tmp_path / "cards" / "_quarantine" / "limit").glob("*.json"))) == 1

    def test_the_count_is_taken_from_names_and_remembered_until_the_folder_changes(self, tmp_path, monkeypatch):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(30)])
        counts = []
        real = FilesystemCardRepository.count
        monkeypatch.setattr(FilesystemCardRepository, "count", lambda self: counts.append(1) or real(self))
        store = store_for(tmp_path, tmp_path, max_cards=10)
        note = {"id": "l", "type": "note", "locator": {"text": "x"}, "why": "w"}
        assert store.update_card("lesson-first", add=[note])         # the first write also makes the report and counters
        counts.clear()
        for i in range(5):
            assert store.update_card(f"lesson-{i}", add=[note])
        assert counts == [], "its own writes keep the count true: nothing is listed again"
        other = store_for(tmp_path, tmp_path, max_cards=10)
        with other.limit_guard.folder:
            other.limit_guard.folder.bump()
        store.update_card("lesson-9", add=[note])
        assert len(counts) == 1, "another process changed the folder: listed once more"


class TestAStoreAlreadyOverItsLimit:
    """Round 2 finding 7: a store above its limit swapped regenerable cards for learned ones, one for one, for good."""

    def test_learned_cards_are_admitted_without_moving_any_card_out(self, tmp_path):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(40)])
        store = store_for(tmp_path, tmp_path, max_cards=10)
        for i in range(5):
            assert store.update_card(f"lesson-{i}", add=[{"id": "l", "type": "note", "locator": {"text": "x"}, "why": "w"}])
        assert len(card_ids(tmp_path / "cards")) == 45
        assert not (tmp_path / "cards" / "_quarantine").exists(), "nothing was moved out"
        assert store.limit_guard.state.admitted_over == 5 and store.limit_guard.state.evicted == 0

    def test_the_report_says_so(self, tmp_path):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(12)])
        store = store_for(tmp_path, tmp_path, max_cards=10)
        store.update_card("lesson-1", add=[{"id": "l", "type": "note", "locator": {"text": "x"}, "why": "w"}])
        assert "kept above the limit (nothing derived could be moved): 1" in report_for(str(tmp_path / "cards"), 10)

    def test_a_store_exactly_at_its_limit_still_makes_room(self, tmp_path):
        seed(tmp_path / "cards", [card(f"topic-{i}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(10)])
        store = store_for(tmp_path, tmp_path, max_cards=10)
        assert store.update_card("lesson-1", add=[{"id": "l", "type": "note", "locator": {"text": "x"}, "why": "w"}])
        assert len(card_ids(tmp_path / "cards")) == 10 and store.limit_guard.state.evicted == 1


def writer_process(cards_dir: str, root: str, number: int, barrier, results) -> None:
    logging.disable(logging.CRITICAL)
    store = FileContextStore(cards_dir, repo_root=root, auto_bootstrap=False, mount_mode="copy", max_cards=100)
    store._load_all()
    store.limit_guard.snapshot()
    barrier.wait()
    written = 0
    for i in range(10):
        written += bool(store.write_card(f"p{number}-learned-{i}", {"name": "l", "content": [NOTE]}))
    results.put(written)


class TestSeveralWritersAtTheLimit:
    """Review finding 9: four processes at 95 of 100 overshot the limit and double-counted the moves."""

    def test_the_limit_holds_and_every_move_is_counted_once(self, tmp_path):
        import multiprocessing as mp
        cards_dir = tmp_path / "cards"
        seed(cards_dir, [card(f"t{i:03d}", files=[f"f{i}/a.md", f"f{i}/b.md"]) for i in range(95)])
        context = mp.get_context("fork")
        barrier, results = context.Barrier(4), context.Queue()
        procs = [context.Process(target=writer_process, args=(str(cards_dir), str(tmp_path), n, barrier, results))
                 for n in range(4)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=120)
        written = sum(results.get() for _ in procs)
        live = [p for p in cards_dir.glob("*.json") if not p.name.startswith("bootstrap")]
        moved = list((cards_dir / "_quarantine" / "limit").glob("*.json"))
        lines = [json.loads(line)["moved"] for line in (cards_dir / "_quarantine" / "limit" / "manifest.jsonl")
                 .read_text().splitlines()]
        assert written == 40
        assert len(live) == 100, "learned cards fill the room the moved topic cards left, and no more"
        assert len(moved) == len(lines) == len(set(lines)) == 35


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

    def test_it_says_what_fills_the_store_in_two_short_lines(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert self.refuse_one(store) is False
        shown = [r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True)]
        assert shown == [
            "Context store full at 10: 4 topic, 2 imported, 3 learned, 1 quest",
            "Run quest-ai-runner cards tidy --apply, or set QAR_MAX_CARDS higher",
        ]
        assert all("—" not in line for line in shown)

    @pytest.mark.parametrize("width", [60, 80, 120])
    def test_both_lines_survive_the_terminals_cut_whatever_the_window(self, tmp_path, caplog, width):
        """Review finding 10: the transcript shows the first sentence of a warning, cut to the window (width - 8);
        the counts and the instruction both have to be on screen in full, and the report path never is."""
        from quest_ai_runner.textual_ui_widgets import LOG_LINE_CHARS, short_log_text
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
        shown = [r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True)]
        limit = min(LOG_LINE_CHARS, width - 8)
        if width >= 80:
            assert [short_log_text(line, limit) for line in shown] == shown, "nothing was cut or ended early"
        assert not any("card_limit_report" in line or "/" in line for line in shown), "the path is in the log only"

    def test_the_counts_fit_one_80_column_line_even_for_the_incident_sized_store(self):
        from hubofhubs.recall.card_limit_report import Breakdown, count_line
        for numbers in ((1000, 1000, 812, 150, 30, 8), (1000, 1430, 1200, 200, 25, 5),
                        (1000, 142781, 130000, 10000, 2000, 781)):
            line = count_line(Breakdown(*numbers), VOICE)
            assert len(line) <= 72 and "topic" in line and "imported" in line and "learned" in line and "quest" in line
            assert ". " not in line, "the terminal ends a warning at the first full stop"

    def test_the_names_of_the_biggest_folders_are_in_the_log_not_on_screen(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
        full = next(r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True) is False)
        assert "the biggest folders: alpha 2, beta 1, gamma 1" in full
        assert "New topic and imported cards are not being added" in full

    def test_a_lane_has_no_screen_so_the_next_task_feed_carries_the_same_two_lines_once(self, tmp_path):
        store = mixed_store(tmp_path)
        self.refuse_one(store)
        notes = card_limit.take_task_notes()
        assert len(notes) == 1
        note = notes[0]
        assert note.startswith("About the context store, not this task: Context store full at 10: 4 topic, 2 imported, "
                               "3 learned, 1 quest. To bring it under the limit run: quest-ai-runner cards tidy "
                               f"--cards-dir {tmp_path / 'cards'} --apply")
        assert note.index("cards tidy") < note.index("QAR_MAX_CARDS higher"), "the tidy first, a higher limit second"
        assert "exported first" in note and "--undo" in note
        assert "cannot be rebuilt" not in note, "a local folder sets a cold card aside, so nothing to explain"
        assert card_limit.take_task_notes() == []

    def test_a_startup_callback_is_told_both_lines_when_the_store_is_opened_full(self, tmp_path):
        said = []
        store = mixed_store(tmp_path)
        store.prewarm(notify=said.append)
        assert said == ["Context store full at 10: 4 topic, 2 imported, 3 learned, 1 quest",
                        "Run quest-ai-runner cards tidy --apply, or set QAR_MAX_CARDS higher"]

    def test_the_log_carries_the_instructions_an_ai_needs_to_change_it(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
        full = next(r.getMessage() for r in caplog.records if getattr(r, "in_transcript", True) is False)
        for needed in ("QAR_MAX_CARDS", "0 (or the word unlimited) means no limit", "env_files", "[env]",
                       "Environment=", "restart", str(tmp_path / "cards" / REPORT_NAME),
                       "quest-ai-runner cards report", "_quarantine", "quest-ai-runner cards tidy"):
            assert needed in full, needed

    def test_it_fires_once_per_process(self, tmp_path, caplog):
        store = mixed_store(tmp_path)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            self.refuse_one(store)
            first = len(caplog.records)
            self.refuse_one(store)
            store._store_write("another", card("another", files=["z/a.md", "z/b.md"]))
        assert first == 3 and len(caplog.records) == 3

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
        assert shown[0] == "Store over limit 5 (12): 12 topic, 0 imported, 0 learned, 0 quest"
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
        assert notice_lines(b)[0].startswith("Context store full at 10:")


# --- the other writers -------------------------------------------------------------------------------

def test_a_merge_that_would_pass_the_limit_is_a_problem_and_applies_nothing(tmp_path):
    from hubofhubs.recall import card_store_identity as ident
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
