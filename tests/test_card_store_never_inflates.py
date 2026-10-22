"""A card store never grows unless its corpus does.

Joshua, 2026-10-22: "we've brought down the context card store to hundreds instead of thousands multiple
times, don't let this happen again." Fixing each route as it was found (2026-10-05: direction of an import)
left the next one open: a store of his reached 142,781 cards. So this does not test one route, it plays a
random world and checks what must hold after every step (``evaluation/components/card_store_growth/world.py``,
which the ESOI arm measures too):

  I1  no card id repeats a namespace segment, and none is a vector point id
  I2  an id never has more ``--`` than the world has nesting levels
  I3  no two derived cards of a store pin exactly the same files
  I4  indexing a store again right away changes nothing, and the settled world is a fixed point
  I5  the settled total rises only with the files, folders and cards the steps added
  I6  no store is over its card limit, unless everything in it was learned or written for a quest

The seeds below are ones the library before this change failed (at least 8 in 10 of any 40 did). Each route
that was found has its own named test after them, with the number it produced.
"""
from __future__ import annotations

import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
from card_store_growth import world  # noqa: E402

SEEDS = (0, 1, 2, 3, 5, 6, 9, 24, 28, 36)
STEPS = 20


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("QAR_CARD_MOUNT", raising=False)
    monkeypatch.delenv("QAR_MAX_CARDS", raising=False)
    with world.offline_git():
        yield


@pytest.mark.parametrize("seed", SEEDS)
def test_no_sequence_of_operations_inflates_a_store(seed):
    outcome = world.run(seed, STEPS)
    assert outcome["violations"] == [], (
        f"seed {seed} broke an invariant.\nFirst: {outcome['violations'][0]}\n"
        f"The world and its steps: {'; '.join(outcome['trace'])}")


def test_the_checks_are_not_vacuous(tmp_path):
    """The shapes of the real incident are caught by the checks, so a green fuzz means something."""
    w = world.World(tmp_path / "w", random.Random(1), depth=2)
    files = ["a/one.md", "a/two.md"]
    bad = ["root-5ec1f7--product-a87921--root-5ec1f7--topic", "card:topic", "plain-one", "plain-two"]
    w.cards[0].mkdir(parents=True, exist_ok=True)
    for cid in bad:
        card = {"id": cid, "files": [{"path": p} for p in files], "provenance": {"created_by_task": "bootstrap"}}
        (w.cards[0] / f"{cid}.json").write_text(json.dumps(card))
    found = "\n".join(world.check(w))
    assert "I1" in found and "repeats a namespace segment" in found
    assert "is a vector point id" in found
    assert "I3" in found and "pin the same files" in found


# --- helpers for the named routes -------------------------------------------------------------------

def notes(root: Path, folders: List[str]) -> Path:
    for name in folders:
        (root / name).mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (root / name / f"n{i}.md").write_text(f"# {name} {i}\nabout {name}\n")
    return root


def open_store(root: Path, cards: Path = None, **kwargs: Any) -> FileContextStore:
    # Arguments an older library does not have are left out, so the same file runs against it ("before").
    wanted = dict(kwargs, max_cards=0)
    return FileContextStore(str(cards or root / ".quest-context"), repo_root=str(root), auto_bootstrap=False,
                            mount_mode="copy", **{k: v for k, v in wanted.items() if world.store_accepts(k)})


def index(root: Path, cards: Path = None, refresh: bool = False, **kwargs: Any) -> int:
    store = open_store(root, cards, **kwargs)
    call = store.refresh_stale if refresh else store.bootstrap
    return call(root=str(root), provider=world.DiskProvider(root.resolve()))


def load(root: Path) -> Dict[str, Dict[str, Any]]:
    return FilesystemCardRepository(str(root / ".quest-context")).load_all()


def provenance(card: Dict[str, Any]) -> Dict[str, Any]:
    return card.get("provenance") or {}


def is_copy(card: Dict[str, Any]) -> bool:
    return bool(provenance(card).get("imported_from") or provenance(card).get("origin_store"))


def store_id_of(root: Path) -> str:
    return json.loads((root / ".quest-context" / "bootstrap_meta.json").read_text())["store_id"]


# --- route: the cards folder sits inside the corpus it indexes -----------------------------------------

def test_a_cards_folder_inside_its_own_corpus_does_not_import_itself(tmp_path):
    """QAR_CONTEXT_CARDS_DIR placed under the corpus root made the store find its own folder as a 'nested
    store' and copy its whole self in on every refresh: 3 cards, then 6, 9, 12, 15, 18 after six passes.
    That is a store of ~400 cards at 350 refreshes (a week of daily restarts and chat starts): 142,781."""
    root = notes(tmp_path / "corpus", ["auth", "chat", "docs"])
    cards = root / "inner" / ".quest-context"
    counts = []
    for n in range(6):
        index(root, cards, refresh=n > 0)
        counts.append(len(FilesystemCardRepository(str(cards)).load_all()))
    assert counts == [3] * 6


def test_a_cards_folder_above_its_corpus_does_not_import_itself(tmp_path):
    root = notes(tmp_path / "home" / "corpus", ["auth", "chat"])
    cards = tmp_path / "home" / ".quest-context"
    for n in range(4):
        index(root, cards, refresh=n > 0)
    assert len(FilesystemCardRepository(str(cards)).load_all()) == 2


# --- route: a copy that lost its provenance --------------------------------------------------------------

def nested_pair(tmp_path: Path):
    parent = notes(tmp_path / "top", ["chat", "docs"])
    child = notes(parent / "org", ["chat", "search"])
    index(child)
    index(parent)
    index(child, refresh=True)
    index(parent, refresh=True)
    return parent, child


def test_a_new_topic_never_merges_into_an_imported_card(tmp_path):
    """The parent's own topic for its ``chat`` folder shares keywords with the card imported from the child's
    ``chat`` folder, and the topic merge folded the one into the other under the IMPORTED id and wrote it
    without ``imported_from``: a prefixed id that every later import took for an original (a 13-prefix id
    was found on a real machine, 9 provenance-less copies in another store)."""
    parent, child = nested_pair(tmp_path)
    for cid, card in load(parent).items():
        if "--" in cid:
            assert provenance(card).get("origin_store"), f"{cid} is a prefixed id that records no origin"
    own = load(parent)["chat-topic"]
    assert not is_copy(own)
    assert any(provenance(c).get("origin_id") == "chat-topic" for c in load(parent).values()), (
        "the child's chat card is still imported, as its own card")


def test_a_store_never_imports_the_copy_of_its_own_card_whatever_happened_to_the_copys_provenance(tmp_path):
    parent, child = nested_pair(tmp_path)
    before = sorted(load(child))
    mine = store_id_of(child)
    assert not [c for c in load(child).values() if provenance(c).get("origin_store") == mine]
    for path in (parent / ".quest-context").glob("*.json"):       # a writer that drops every copy's provenance
        card = json.loads(path.read_text())
        if isinstance(card.get("provenance"), dict) and card["provenance"].pop("imported_from", None):
            path.write_text(json.dumps(card))
    for _ in range(3):
        index(child, refresh=True)
        index(parent, refresh=True)
    assert sorted(load(child)) == before


# --- route: the same card by two ways --------------------------------------------------------------------

def test_the_same_card_reached_by_two_routes_is_one_id(tmp_path):
    """sd imported product's card (``sd-...--product-...--card`` for the store above); when sd had no store
    yet the store above imported it directly under another id, so both stayed."""
    via_middle = tmp_path / "a" / "top"
    notes(via_middle, ["docs", "x"])
    leaf = notes(via_middle / "mid" / "leaf", ["auth", "search"])
    index(leaf)
    index(via_middle / "mid")
    index(via_middle)
    direct = tmp_path / "b" / "top"
    notes(direct, ["docs", "x"])
    leaf2 = notes(direct / "mid" / "leaf", ["auth", "search"])
    index(leaf2)
    index(direct)

    def from_leaf(top: Path) -> List[str]:
        return sorted(cid for cid, c in load(top).items() if provenance(c).get("origin_id") in ("auth-topic", "search-topic"))
    assert from_leaf(via_middle) == from_leaf(direct)
    assert len(from_leaf(via_middle)) == 2
    assert all(cid.count("--") == 1 for cid in from_leaf(via_middle)), "an id never grows by hop"


# --- route: indexing under another root --------------------------------------------------------------------

def test_a_store_is_not_indexed_under_a_root_it_was_not_built_for(tmp_path, caplog):
    """Two lanes sharing one cards folder, or a lane whose root was widened without moving the store, made
    each lane find the other's cards dead and re-card what the other had just lost."""
    root = notes(tmp_path / "top", ["auth", "chat"])
    sub = notes(root / "sub", ["search", "export"])
    index(root)
    before = sorted(load(root))
    with caplog.at_level(logging.ERROR, logger="quest-ai-runner.context"):
        assert index(sub, root / ".quest-context", refresh=True) == 0
    assert "built for the corpus root" in caplog.text and "move-root" in caplog.text
    assert sorted(load(root)) == before


# --- route: a vector point id used as a card id ------------------------------------------------------------

@pytest.mark.parametrize("name", ["card:auth-topic", "card:st_0123456789abcdef:auth-topic"])
def test_a_vector_point_id_names_the_card_it_stands_for(tmp_path, name):
    """The vector arm lists hits as ``card:<id>``; the post-deep updater wrote its notes to a NEW card under
    that name (17 of them in one real store, 3 in another)."""
    root = notes(tmp_path / "top", ["auth"])
    index(root)
    assert open_store(root).add_content(name, {"type": "note", "locator": {"text": "learned"}, "why": "w"})
    assert sorted(load(root)) == ["auth-topic"]
    assert [i["locator"]["text"] for i in load(root)["auth-topic"]["content"]] == ["learned"]


# --- the write-time invariant -----------------------------------------------------------------------------

def test_the_filesystem_repository_refuses_an_id_that_repeats_a_namespace(tmp_path, caplog):
    repo = FilesystemCardRepository(str(tmp_path / "cards"))
    with caplog.at_level(logging.ERROR, logger="quest-ai-runner.card-store"):
        assert repo.write("sd-aaaaaa--root-bbbbbb--sd-aaaaaa--topic", {"id": "x"}) is False
        assert repo.write("card:topic", {"id": "x"}) is False
        assert repo.write("sd-aaaaaa--topic", {"id": "sd-aaaaaa--topic"}) is True
    assert "sd-aaaaaa" in caplog.text and "appears twice" in caplog.text and "vector point is keyed" in caplog.text
    assert sorted(repo.load_all()) == ["sd-aaaaaa--topic"]


def test_the_store_write_path_refuses_it_and_the_callers_that_promise_not_to_raise_do_not(tmp_path):
    store = open_store(tmp_path, tmp_path / "cards")
    assert store.write_card("a--b--a", {"id": "a--b--a", "summary": "x"}) is False
    assert sorted(FilesystemCardRepository(str(tmp_path / "cards")).load_all()) == []


def test_the_quest_backend_repository_refuses_it_too_and_so_does_a_bulk_upload():
    from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
    from tests.quest_card_backend_fake import FakeCardBackend
    backend = FakeCardBackend()
    try:
        repo = QuestApiCardRepository(base_url=backend.url, api_key="k")
        assert repo.write("a--b--a", {"id": "a--b--a", "name": "x", "summary": "x"}) is False
        result = repo.bulk_write([("a--b--a", {"name": "x"}), ("fine", {"name": "y", "summary": "y"})])
        assert result["failed"] == ["a--b--a"] and result["counts"]["failed"] >= 1
        assert "a--b--a" not in repo.load_all() and "fine" in repo.load_all()
    finally:
        backend.close()


# --- old stores heal ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("with_provenance", [True, False], ids=["copies-that-say-so", "copies-that-lost-it"])
def test_old_compounded_copies_leave_on_the_first_refresh(tmp_path, with_provenance):
    """Both kinds: copies still marked as imports (no longer offered under the new ids, so retired) and copies
    that lost their mark (a card of the store whose files they pin makes them provably redundant)."""
    top = notes(tmp_path / "top", ["docs", "x"])
    mid = notes(top / "mid", ["auth", "search"])
    index(mid)
    index(top)
    healthy = sorted(load(top))
    planted = world.plant_legacy_copies(top, with_provenance)
    assert planted and len(load(top)) == len(healthy) + planted
    start = open_store(top, self_heal=True)          # what a runner does on its first start: open, then refresh
    start.prewarm()
    start.refresh_stale(root=str(top), provider=world.DiskProvider(top.resolve()))
    assert sorted(load(top)) == healthy
    quarantine = top / ".quest-context" / "_quarantine"
    kept = sorted(p.name for p in quarantine.glob("*/*.json"))
    assert len(kept) == planted, "every one of them is still on disk, moved, not deleted"


# --- when a card was first written ----------------------------------------------------------------------

def test_created_at_is_stamped_when_a_card_is_first_written_and_never_again(tmp_path):
    root = notes(tmp_path / "top", ["auth"])
    index(root)
    first = provenance(load(root)["auth-topic"])["created_at"]
    assert first and first.endswith("+00:00")
    (root / "auth" / "n0.md").write_text("# changed\n")
    index(root, refresh=True)
    again = provenance(load(root)["auth-topic"])
    assert again["created_at"] == first and again["last_verified_at"] == ""
    store = open_store(root)
    store.record("fix the auth bug", {"kind": "met", "files": []})
    store.record("fix the auth bug", {"kind": "met", "files": [], "verified_at": "2026-10-22"})
    learned = [c for c in load(root).values() if not provenance(c).get("created_by_task") == "bootstrap"]
    assert len(learned) == 1
    assert provenance(learned[0])["created_at"] and provenance(learned[0])["last_verified_at"] == "2026-10-22"


def test_a_card_written_before_dates_were_kept_stays_undated(tmp_path):
    root = notes(tmp_path / "top", ["auth"])
    index(root)
    path = root / ".quest-context" / "auth-topic.json"
    card = json.loads(path.read_text())
    card["provenance"]["created_at"] = ""
    path.write_text(json.dumps(card))
    open_store(root).add_content("auth-topic", {"type": "note", "locator": {"text": "n"}, "why": "w"})
    assert provenance(load(root)["auth-topic"]).get("created_at") == ""


def test_a_card_learned_from_a_run_that_pins_files_stays_learned_through_a_refresh(tmp_path):
    """The pass that adds term signatures to a card's files rewrote a card learned from a run (which pins
    files) as a ``bootstrap`` card with none of its notes: the card then counted as regenerable, and lost
    what the run taught it."""
    root = notes(tmp_path / "top", ["auth"])
    index(root)
    store = open_store(root)
    store.record("fix the auth bug", {"kind": "met", "files": ["auth/n0.md", "auth/n1.md"]})
    learned_id = next(cid for cid, c in load(root).items() if provenance(c).get("created_by_task") != "bootstrap")
    store.add_content(learned_id, {"type": "note", "locator": {"text": "taught by a run"}, "why": "w"})
    (root / "auth" / "n0.md").write_text("# changed\n")
    index(root, refresh=True)
    card = load(root)[learned_id]
    assert provenance(card)["created_by_task"] == "fix the auth bug"
    assert [i["locator"]["text"] for i in card["content"]] == ["taught by a run"]
    assert card["usage_count"] == 1
