"""Card-store identity, read-only mounting, move-root, migrate-imports, and the vector fence.

Everything runs offline on temp directories; the vector tests use the real embedded Qdrant engine
with a counting toy embedder (skipped when qdrant-client is not installed).

The measurable claims, each asserted below:
  * re-embeds on a root move               = 0
  * points per distinct card, any number of mounting corpora = 1
  * cards copied between stores in mount mode = 0
  * a store the reader cannot read is not mounted and is not indexed by the reader either
  * a reading lane never writes into a mounted store (cards, usage stamps, vectors)
  * a change in the owner store is visible to the reader with zero reader writes
  * the vector arm returns points only of the stores actually mounted
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from hubofhubs.recall import card_store_identity as ident
from quest_ai_runner.adapters import card_store_fence as fence
from quest_ai_runner.adapters import card_store_migrate as migrate
from hubofhubs.recall.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore, write_bootstrap_meta
from quest_ai_runner.core.vector_scopes import CARD_SEED_SCOPE


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def card(card_id: str, files: List[str], keywords: Optional[List[str]] = None,
         summary: str = "") -> Dict[str, Any]:
    return {
        "id": card_id,
        "name": card_id.replace("-", " ").title(),
        "keywords": keywords or [card_id],
        "summary": summary or f"About {card_id}",
        "description": summary or f"About {card_id}",
        "files": [{"path": f, "sha256": "", "mtime": 0} for f in files],
        "provenance": {"created_by_task": "bootstrap"},
    }


def make_owner(root: Path, cards: List[Dict[str, Any]], *, with_files: bool = True) -> Path:
    """A bootstrapped store under ``root``: cards in ``root/.quest-context`` plus a real identity."""
    cards_dir = root / ".quest-context"
    cards_dir.mkdir(parents=True, exist_ok=True)
    repo = FilesystemCardRepository(str(cards_dir))
    for c in cards:
        repo.write(c["id"], c)
        if with_files:
            for fe in c["files"]:
                target = root / fe["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("def f():\n    pass\n", encoding="utf-8")
    write_bootstrap_meta(str(cards_dir), len(cards), root=str(root))
    return cards_dir


def snapshot(directory: Path) -> Dict[str, Any]:
    """Every file under ``directory`` with its mtime and size: equal snapshots mean no writes."""
    out = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(directory))] = (st.st_mtime_ns, st.st_size)
    return out


def reader_store(root: Path, **kw) -> FileContextStore:
    return FileContextStore(str(root / ".quest-context"), repo_root=str(root),
                            auto_bootstrap=False, mount_mode="mount", confidence_threshold=0.0, **kw)


def ids_of(store: FileContextStore) -> List[str]:
    return sorted(store._load_all().keys())


def settle() -> None:
    """Step past the filesystem's timestamp granularity so successive writes get distinct stamps.

    The change marker a reader watches is the owner's directory stamp; two writes inside one
    timestamp tick share it (the repository's periodic full re-scan backstops that case).
    """
    time.sleep(0.05)


def provider_recording_prompts():
    provider = MagicMock()
    provider.list_models.return_value = []
    provider.answer.return_value = "[]"
    return provider


# --------------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------------

class TestStoreIdentity:
    def test_new_store_gets_a_qualified_id_once_and_keeps_it(self, tmp_path):
        d = make_owner(tmp_path / "a", [card("c1", ["x.py"])])
        meta = ident.read_meta(str(d))
        assert meta["store_id"].startswith("st_")
        assert meta["vector_id_scheme"] == ident.SCHEME_QUALIFIED
        assert meta["root"] == str(tmp_path / "a")
        # A later bootstrap pass rewrites the pass keys but never the identity.
        write_bootstrap_meta(str(d), 5, root=str(tmp_path / "elsewhere"))
        again = ident.read_meta(str(d))
        assert again["store_id"] == meta["store_id"]
        assert again["root"] == meta["root"], "a bootstrap never moves the recorded root"
        assert again["card_count"] == 5

    def test_pre_identity_store_is_legacy_so_its_points_keep_their_ids(self, tmp_path):
        d = tmp_path / "old" / ".quest-context"
        d.mkdir(parents=True)
        (d / "bootstrap_meta.json").write_text(json.dumps({"version": 7, "card_count": 3}))
        sid = ident.ensure_store_id(str(d))
        meta = ident.read_meta(str(d))
        assert sid == meta["store_id"]
        assert meta["vector_id_scheme"] == ident.SCHEME_LEGACY
        assert meta["version"] == 7 and meta["card_count"] == 3, "other keys are preserved"

    def test_id_is_not_derived_from_the_path(self, tmp_path):
        a = make_owner(tmp_path / "one", [card("c1", ["x.py"])])
        b = make_owner(tmp_path / "two", [card("c1", ["x.py"])])
        assert ident.store_id_of(str(a)) != ident.store_id_of(str(b))
        moved = tmp_path / "renamed"
        (tmp_path / "one").rename(moved)
        assert ident.store_id_of(str(moved / ".quest-context")) == \
            ident.read_meta(str(moved / ".quest-context"))["store_id"]

    def test_ensure_store_id_never_invents_a_meta(self, tmp_path):
        d = tmp_path / "cards"
        d.mkdir()
        assert ident.ensure_store_id(str(d)) is None
        assert not (d / "bootstrap_meta.json").exists(), \
            "that file's presence means 'completed its own bootstrap' to other corpora"


# --------------------------------------------------------------------------------------------
# mount: read in place, prefix at read time, nothing copied
# --------------------------------------------------------------------------------------------

class TestMountRead:
    def test_nested_store_is_read_in_place_with_prefix_and_zero_copies(self, tmp_path):
        owner_dir = make_owner(tmp_path / "wide" / "proj", [card("models", ["pkg/models.py"])])
        sid = ident.store_id_of(str(owner_dir))
        reader = reader_store(tmp_path / "wide")
        assert reader.bootstrap(root=str(tmp_path / "wide")) == 0

        cards = reader._load_all()
        assert list(cards) == [f"{sid}:models"]
        view = cards[f"{sid}:models"]
        assert [fe["path"] for fe in view["files"]] == ["proj/pkg/models.py"]
        assert view["mounted_from"] == {"store_id": sid, "card_id": "models"}
        # cards copied between stores in mount mode = 0
        on_disk = [p for p in (tmp_path / "wide" / ".quest-context").glob("*.json")
                   if p.name != "bootstrap_meta.json"]
        assert on_disk == []
        # the owner's own file still says what it always said: relative to ITS root
        stored = json.loads((owner_dir / "models.json").read_text())
        assert stored["files"][0]["path"] == "pkg/models.py"

    def test_ancestor_store_is_mounted_and_trimmed_to_the_readers_root(self, tmp_path):
        top = tmp_path / "top"
        make_owner(top, [card("both", ["lane/a.py", "other/b.py"]), card("elsewhere", ["other/c.py"])])
        (top / "lane").mkdir(exist_ok=True)
        reader = reader_store(top / "lane")
        reader.bootstrap(root=str(top / "lane"))
        views = [c for c in reader._load_all().values()]
        assert len(views) == 1, "a card with nothing under the reader's root is not shown"
        assert [fe["path"] for fe in views[0]["files"]] == ["a.py"]

    def test_assemble_serves_a_mounted_card_and_the_owner_is_not_written(self, tmp_path):
        owner_dir = make_owner(tmp_path / "wide" / "proj",
                               [card("billing", ["inv.py"], keywords=["invoice", "billing"])])
        reader = reader_store(tmp_path / "wide")
        reader.bootstrap(root=str(tmp_path / "wide"))
        before = snapshot(owner_dir)
        result = reader.assemble("how do invoice and billing work", meta={})
        assert result.card_ids and result.card_ids[0].endswith(":billing")
        assert snapshot(owner_dir) == before, "assembling must not stamp usage into the owner's store"

    def test_default_mode_is_still_copy(self, tmp_path):
        store = FileContextStore(str(tmp_path / "c"), repo_root=str(tmp_path), auto_bootstrap=False)
        assert store.mounts_enabled is False


class TestFreshness:
    def test_owner_changes_reach_the_reader_with_zero_reader_writes(self, tmp_path):
        wide = tmp_path / "wide"
        owner_dir = make_owner(wide / "proj", [card("one", ["a.py"], summary="first")])
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        reader_dir = wide / ".quest-context"
        sid = ident.store_id_of(str(owner_dir))
        assert ids_of(reader) == [f"{sid}:one"]
        reader_before = snapshot(reader_dir)

        owner = FilesystemCardRepository(str(owner_dir))
        settle()
        owner.write("two", card("two", ["b.py"], summary="added by the owner"))
        assert ids_of(reader) == sorted([f"{sid}:one", f"{sid}:two"]), "a new owner card is visible"

        updated = card("one", ["a.py"], summary="rewritten by the owner")
        settle()
        owner.write("one", updated)
        assert reader._load_all()[f"{sid}:one"]["summary"] == "rewritten by the owner"

        settle()
        owner.delete("two")
        assert ids_of(reader) == [f"{sid}:one"]
        assert snapshot(reader_dir) == reader_before, "the reader wrote nothing at all"


# --------------------------------------------------------------------------------------------
# the reader's indexing pass leaves a mounted subtree alone
# --------------------------------------------------------------------------------------------

class TestReaderDoesNotIndexMountedSubtree:
    def _wide_with_mount_and_own_file(self, tmp_path):
        wide = tmp_path / "wide"
        make_owner(wide / "proj", [card("models", ["pkg/models.py"])])
        (wide / "mine.py").write_text("def mine():\n    pass\n", encoding="utf-8")
        return wide

    def test_bootstrap_never_walks_cards_or_prompts_for_mounted_files(self, tmp_path):
        wide = self._wide_with_mount_and_own_file(tmp_path)
        (wide / "proj" / "pkg" / "extra_uncovered.py").write_text("x = 1\n", encoding="utf-8")
        provider = provider_recording_prompts()
        provider.answer.return_value = json.dumps([{
            "id": "mine", "name": "Mine", "keywords": ["mine"], "summary": "own", "files": ["mine.py"]}])
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide), provider=provider)
        prompts = json.dumps([c.args for c in provider.answer.call_args_list])
        assert "proj/" not in prompts and "models.py" not in prompts and "extra_uncovered" not in prompts, \
            "no file of the mounted subtree may reach the indexing model"
        own = [c for c in reader._own_cards().values()]
        for c in own:
            for fe in c["files"]:
                assert not fe["path"].startswith("proj/"), "no card is made for the mounted subtree"

    def test_unreadable_store_is_skipped_logged_and_not_indexed_instead(self, tmp_path, caplog):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        wide = self._wide_with_mount_and_own_file(tmp_path)
        locked = wide / "proj" / ".quest-context"
        provider = provider_recording_prompts()
        provider.answer.return_value = "[]"
        os.chmod(locked, 0o000)
        try:
            reader = reader_store(wide)
            with caplog.at_level("WARNING"):
                reader.bootstrap(root=str(wide), provider=provider)
            assert reader.live_mounts() == []
            assert [c for c in reader._load_all()] == [], "no cards for the unreadable subtree"
            prompts = json.dumps([c.args for c in provider.answer.call_args_list])
            assert "models.py" not in prompts, "it must NOT fall back to indexing those files itself"
            assert any("permission denied" in r.getMessage() for r in caplog.records), \
                "a permission error is reported, not swallowed"
        finally:
            os.chmod(locked, 0o755)

    def test_unreadable_ancestor_is_invisible_and_the_reader_indexes_its_own_root(self, tmp_path):
        # A narrower lane under a wider store it cannot read (a different OS user's store above it)
        # must keep indexing its own files: the wider store is not its to mount, and its owner is
        # the one that mounts this lane's store, not the other way round.
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        top = tmp_path / "top"
        make_owner(top, [card("t", ["lane/a.py"])])
        (top / "lane").mkdir(exist_ok=True)
        (top / "lane" / "a.py").write_text("x = 1\n")
        provider = provider_recording_prompts()
        os.chmod(top / ".quest-context", 0o000)
        try:
            reader = reader_store(top / "lane")
            reader.bootstrap(root=str(top / "lane"), provider=provider)
            assert reader.live_mounts() == []
            assert provider.answer.call_count > 0, "the reader still indexes its own root"
        finally:
            os.chmod(top / ".quest-context", 0o755)

    def test_store_without_identity_is_not_mounted(self, tmp_path):
        wide = tmp_path / "wide"
        d = wide / "proj" / ".quest-context"
        d.mkdir(parents=True)
        (d / "bootstrap_meta.json").write_text(json.dumps({"version": 1}))
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        assert reader.live_mounts() == []
        decision = ident.check_readable(str(d))
        assert not decision.allowed and "store_id" in decision.reason


# --------------------------------------------------------------------------------------------
# write isolation
# --------------------------------------------------------------------------------------------

class TestWriteIsolation:
    def test_every_write_path_refuses_a_mounted_card(self, tmp_path):
        wide = tmp_path / "wide"
        owner_dir = make_owner(wide / "proj", [card("shared", ["a.py"])])
        sid = ident.store_id_of(str(owner_dir))
        mid = f"{sid}:shared"
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        reader._load_all()
        before = snapshot(owner_dir)

        assert reader.write_card(mid, card("shared", ["a.py"])) is False
        assert reader.update_card(mid, fields={"name": "hijacked"}) is False
        assert reader.add_content(mid, {"id": "n", "type": "note", "text": "x"}) is False
        assert reader.remove_content(mid, "n") is False
        assert reader.mark_sources_used(mid, file_paths=["proj/a.py"]) is False
        assert reader._repo.delete(mid) is False
        assert reader._repo.write(mid, card("shared", ["a.py"])) is False
        assert snapshot(owner_dir) == before

    def test_reader_output_lands_in_its_own_store_only(self, tmp_path):
        wide = tmp_path / "wide"
        owner_dir = make_owner(wide / "proj", [card("shared", ["a.py"])])
        before = snapshot(owner_dir)
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        reader.record("a brand new task", {"summary": "did a thing", "files": ["mine.py"], "ok": True})
        assert snapshot(owner_dir) == before
        assert reader._own_cards(), "the learned card is the reader's own"


# --------------------------------------------------------------------------------------------
# move-root
# --------------------------------------------------------------------------------------------

class TestMoveRoot:
    def test_dry_run_changes_nothing_and_apply_changes_only_the_root(self, tmp_path):
        old = tmp_path / "old"
        d = make_owner(old, [card("c1", ["a/x.py"]), card("c2", ["b/y.py"])])
        new = tmp_path / "new"
        (new / "a").mkdir(parents=True); (new / "b").mkdir()
        (new / "a" / "x.py").write_text("1"); (new / "b" / "y.py").write_text("2")
        sid = ident.store_id_of(str(d))
        cards_before = {p.name: p.read_bytes() for p in d.glob("c*.json")}

        dry = ident.plan_move_root(str(d), str(new))
        assert dry.ok and dry.card_count == 2 and dry.refs_present_at_new_root == 2
        assert (dry.card_ids_changed, dry.relative_paths_changed, dry.reembeds) == (0, 0, 0)
        assert ident.read_meta(str(d))["root"] == str(old), "a dry run writes nothing"

        applied = ident.apply_move_root(str(d), str(new))
        assert applied.ok
        meta = ident.read_meta(str(d))
        assert meta["root"] == str(new) and meta["store_id"] == sid
        assert meta["root_history"][-1]["root"] == str(old)
        assert {p.name: p.read_bytes() for p in d.glob("c*.json")} == cards_before, \
            "ids and relative paths are untouched"

    def test_refuses_a_missing_target_unless_forced(self, tmp_path):
        d = make_owner(tmp_path / "old", [card("c1", ["a.py"])])
        plan = ident.apply_move_root(str(d), str(tmp_path / "does-not-exist"))
        assert not plan.ok
        assert ident.read_meta(str(d))["root"] == str(tmp_path / "old")

    def test_a_reader_follows_the_owners_move_with_no_change_to_its_own_store(self, tmp_path):
        wide = tmp_path / "wide"
        owner_dir = make_owner(wide / "proj", [card("c1", ["a.py"])])
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        sid = ident.store_id_of(str(owner_dir))
        assert [fe["path"] for fe in reader._load_all()[f"{sid}:c1"]["files"]] == ["proj/a.py"]
        # the owner's corpus moves one level down (cards dir stays where it is, root is re-recorded)
        (wide / "proj" / "deeper").mkdir()
        (wide / "proj" / "deeper" / "a.py").write_text("1")
        ident.apply_move_root(str(owner_dir), str(wide / "proj" / "deeper"))
        assert [fe["path"] for fe in reader._load_all()[f"{sid}:c1"]["files"]] == ["proj/deeper/a.py"]


# --------------------------------------------------------------------------------------------
# migrate-imports
# --------------------------------------------------------------------------------------------

class TestMigrateImports:
    def _copy_mode_reader_with_copies(self, tmp_path):
        wide = tmp_path / "wide"
        owner_dir = make_owner(wide / "proj", [card("models", ["pkg/models.py"])])
        copier = FileContextStore(str(wide / ".quest-context"), repo_root=str(wide),
                                  auto_bootstrap=False, mount_mode="copy")
        assert copier.bootstrap(root=str(wide)) == 1
        copies = [c["id"] for c in copier._load_all().values()]
        assert len(copies) == 1 and "--" in copies[0]
        return wide, owner_dir, copies

    def test_dry_run_reports_and_writes_nothing(self, tmp_path):
        wide, owner_dir, copies = self._copy_mode_reader_with_copies(tmp_path)
        before = snapshot(wide / ".quest-context")
        plan = migrate.plan_migrate_imports(str(wide / ".quest-context"), None)
        assert plan.copies == copies and len(plan.mounts) == 1 and not plan.problems
        assert plan.orphan_point_ids == [f"card:{plan.own_store_id}:{copies[0]}"]
        assert "dry run" in plan.render()
        assert snapshot(wide / ".quest-context") == before

    def test_apply_turns_copies_into_a_mount_without_losing_the_card(self, tmp_path):
        wide, owner_dir, copies = self._copy_mode_reader_with_copies(tmp_path)
        plan = migrate.plan_migrate_imports(str(wide / ".quest-context"), None)
        done = migrate.apply_migrate_imports(plan)
        assert done.applied
        assert not (wide / ".quest-context" / f"{copies[0]}.json").exists()
        meta = ident.read_meta(str(wide / ".quest-context"))
        assert meta["store_id"] and [m["store_id"] for m in meta["mounts"]] == \
            [ident.store_id_of(str(owner_dir))]
        reader = reader_store(wide)
        views = list(reader._load_all().values())
        assert len(views) == 1 and views[0]["files"][0]["path"] == "proj/pkg/models.py"

    def test_a_copy_whose_source_is_not_mountable_is_kept(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        wide, owner_dir, copies = self._copy_mode_reader_with_copies(tmp_path)
        os.chmod(owner_dir, 0o000)
        try:
            plan = migrate.plan_migrate_imports(str(wide / ".quest-context"), None)
            assert plan.copies == [] and [c for c, _ in plan.kept_copies] == copies
            assert plan.refused
        finally:
            os.chmod(owner_dir, 0o755)


# --------------------------------------------------------------------------------------------
# QuestApiCardRepository identity
# --------------------------------------------------------------------------------------------

def test_quest_api_repository_carries_a_store_identity():
    from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
    repo = QuestApiCardRepository("https://quest.example.test", "k", "u1", store_id="api-store-1")
    assert repo.store_id == "api-store-1"
    assert QuestApiCardRepository("https://quest.example.test", "k", "u1").store_id == ""


# --------------------------------------------------------------------------------------------
# vectors: one point per card, queried by store, fenced to what is mounted
# --------------------------------------------------------------------------------------------

pytest.importorskip("qdrant_client")
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore  # noqa: E402

VEC = 8


class CountingEmbedder:
    def __init__(self):
        self.texts = 0

    def __call__(self, texts):
        self.texts += len(texts)
        out = []
        for t in texts:
            v = [0.0] * VEC
            for i, ch in enumerate(t.encode()):
                v[i % VEC] += (ch % 31) / 31.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture()
def qdrant(tmp_path):
    emb = CountingEmbedder()
    store = QdrantVectorStore(path=str(tmp_path / "q"), embedder=emb, vector_size=VEC)
    return store, emb


def point_count(store: QdrantVectorStore) -> int:
    try:
        return int(store._client.count(collection_name=store._collection_name(), exact=True).count)
    except ValueError:  # no collection yet: nothing was ever written
        return 0


def seed(store, owner: FileContextStore) -> int:
    return store.sync(owner.export_for_embedding(), scope=CARD_SEED_SCOPE)


class TestVectorMounting:
    def _owner(self, wide: Path, name: str, cards: List[Dict[str, Any]]):
        d = make_owner(wide / name, cards)
        return FileContextStore(str(d), repo_root=str(wide / name), auto_bootstrap=False)

    def test_a_card_is_embedded_once_however_many_corpora_mount_it(self, tmp_path, qdrant):
        store, emb = qdrant
        shared = tmp_path / "shared"
        owner = self._owner(shared, "proj", [card("alpha", ["sub/a.py"], summary="alpha billing"),
                                              card("beta", ["sub/b.py"], summary="beta invoices")])
        assert seed(store, owner) == 2
        embedded_after_owner = emb.texts

        # Two different corpora mount the SAME store: one sits above it, one inside it.
        above = reader_store(shared)
        above.bootstrap(root=str(shared))
        inside_root = shared / "proj" / "sub"
        inside_root.mkdir(parents=True, exist_ok=True)
        inside = reader_store(inside_root)
        inside.bootstrap(root=str(inside_root))
        for r in (above, inside):
            assert r.export_for_embedding() == [], "a mounted store's cards are not exported for embedding"
            assert store.sync(r.export_for_embedding(), scope=CARD_SEED_SCOPE) == 0

        assert emb.texts == embedded_after_owner, "mounting embeds nothing"
        assert point_count(store) == 2, "points per distinct card, across all mounting corpora = 1"

        sid = ident.store_id_of(str(shared / "proj" / ".quest-context"))
        for reader, expected_path in ((above, "proj/sub/a.py"), (inside, "a.py")):
            hits = fence.StoreFencedVectorStore(store, reader).search("alpha billing", top_k=5)
            assert {h.id for h in hits} == {f"card:{sid}:alpha", f"card:{sid}:beta"}
            alpha = next(h for h in hits if h.id.endswith(":alpha"))
            assert alpha.payload["paths"] == [expected_path], "paths are rebased at read time"

    def test_reader_cannot_get_points_of_a_store_it_did_not_mount(self, tmp_path, qdrant):
        store, _emb = qdrant
        mounted = self._owner(tmp_path / "w", "mine", [card("visible", ["a.py"], summary="visible card")])
        stranger = self._owner(tmp_path / "other", "theirs", [card("secret", ["s.py"], summary="secret card")])
        seed(store, mounted)
        seed(store, stranger)
        assert point_count(store) == 2

        wide = tmp_path / "w"
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        fenced = fence.StoreFencedVectorStore(store, reader)
        found = fenced.search("secret card visible card", top_k=10)
        texts = " ".join(h.text for h in found)
        assert "visible" in texts and "secret" not in texts
        # unfenced, the same server would have answered with the stranger's card
        assert any("secret" in h.text for h in store.search("secret card visible card", top_k=10,
                                                            scope=CARD_SEED_SCOPE) + store.search("secret card", top_k=10))

    def test_untagged_legacy_card_points_are_not_served_to_a_fenced_reader(self, tmp_path, qdrant):
        store, _emb = qdrant
        wide = tmp_path / "w"
        self._owner(wide, "proj", [card("c", ["a.py"])])
        legacy_item = {"id": "card:orphan", "text": "orphan legacy text", "payload": {"paths": ["z.py"]},
                       "fingerprint": "f"}
        store.sync([legacy_item], scope=CARD_SEED_SCOPE)
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        fenced = fence.StoreFencedVectorStore(store, reader)
        assert not [h for h in fenced.search("orphan legacy text", top_k=5) if "orphan" in h.id]

    def test_sync_stamps_an_existing_point_with_its_store_without_re_embedding(self, tmp_path, qdrant):
        store, emb = qdrant
        d = make_owner(tmp_path / "o", [card("c", ["a.py"])])
        # a store that predates identity: strip the id so it embeds its points the legacy way
        meta = ident.read_meta(str(d)); sid = meta.pop("store_id"); meta["vector_id_scheme"] = "legacy"
        meta.pop("created_at", None); ident.write_meta(str(d), meta)
        owner = FileContextStore(str(d), repo_root=str(tmp_path / "o"), auto_bootstrap=False)
        assert seed(store, owner) == 1
        spent = emb.texts
        ident.ensure_store_id(str(d))
        owner = FileContextStore(str(d), repo_root=str(tmp_path / "o"), auto_bootstrap=False)
        items = owner.export_for_embedding()
        assert items[0]["id"] == "card:c", "a legacy store keeps its point ids"
        assert seed(store, owner) == 0, "same content: nothing is re-embedded"
        assert emb.texts == spent
        pts = store._client.scroll(collection_name=store._collection_name(), limit=10, with_payload=True)[0]
        assert [p.payload.get("store_id") for p in pts] == [ident.store_id_of(str(d))]

    def test_task_associations_are_attributed_to_their_lane_and_not_served_to_others(self, tmp_path, qdrant):
        store, _emb = qdrant
        lane_a, lane_b = tmp_path / "a", tmp_path / "b"
        for lane in (lane_a, lane_b):
            make_owner(lane, [card("c", ["x.py"])])
        a = FileContextStore(str(lane_a / ".quest-context"), repo_root=str(lane_a),
                             auto_bootstrap=False, mount_mode="mount")
        b = FileContextStore(str(lane_b / ".quest-context"), repo_root=str(lane_b),
                             auto_bootstrap=False, mount_mode="mount")
        fa, fb = fence.StoreFencedVectorStore(store, a), fence.StoreFencedVectorStore(store, b)
        fa.upsert([{"id": "assoc:quarterly", "text": "quarterly budget review task",
                    "payload": {"task": "quarterly budget review", "paths": ["a.py"]}}])
        # an association written before the fence existed: no store id on it
        store.upsert([{"id": "assoc:legacy", "text": "legacy budget review task",
                       "payload": {"task": "legacy budget review", "paths": ["l.py"]}}])
        assert [h.id for h in fa.search("quarterly budget review task", top_k=10)] == ["assoc:quarterly"]
        assert fb.search("quarterly budget review task", top_k=10) == [], \
            "another lane's task memory is not served through a shared server"
        assert not [h for h in fb.search("legacy budget review task", top_k=10) if h.id == "assoc:legacy"]
        opted_in = fence.StoreFencedVectorStore(store, b, include_unattributed=True)
        assert "assoc:legacy" in {h.id for h in opted_in.search("legacy budget review task", top_k=10)}

    def test_a_reader_cannot_write_vector_items_for_a_mounted_store(self, tmp_path, qdrant):
        store, _emb = qdrant
        wide = tmp_path / "w"
        owner = self._owner(wide, "proj", [card("c", ["a.py"])])
        reader = reader_store(wide)
        reader.bootstrap(root=str(wide))
        sid = ident.store_id_of(str(wide / "proj" / ".quest-context"))
        fenced = fence.StoreFencedVectorStore(store, reader)
        forged = {"id": f"card:{sid}:forged", "text": "forged", "payload": {"store_id": sid},
                  "fingerprint": "x"}
        assert fenced.sync([forged], scope=CARD_SEED_SCOPE) == 0
        assert point_count(store) == 0

    def test_move_root_re_embeds_nothing(self, tmp_path, qdrant):
        store, emb = qdrant
        old = tmp_path / "old"
        d = make_owner(old, [card("c1", ["a/x.py"], summary="one"), card("c2", ["b/y.py"], summary="two")])
        owner = FileContextStore(str(d), repo_root=str(old), auto_bootstrap=False)
        assert seed(store, owner) == 2
        spent, points = emb.texts, point_count(store)
        new = tmp_path / "new"
        shutil_tree(old, new)
        assert ident.apply_move_root(str(d), str(new)).ok
        moved = FileContextStore(str(d), repo_root=str(new), auto_bootstrap=False)
        assert seed(store, moved) == 0, "re-embeds on a root move = 0"
        assert emb.texts == spent and point_count(store) == points

    def test_orphan_points_of_converted_copies_are_listed_and_pruned(self, tmp_path, qdrant):
        store, _emb = qdrant
        wide = tmp_path / "wide"
        make_owner(wide / "proj", [card("models", ["pkg/models.py"], summary="models")])
        copier = FileContextStore(str(wide / ".quest-context"), repo_root=str(wide),
                                  auto_bootstrap=False, mount_mode="copy")
        copier.bootstrap(root=str(wide))
        assert seed(store, copier) == 1, "the old behaviour embedded the copy"
        plan = migrate.plan_migrate_imports(str(wide / ".quest-context"), None, vector_store=store)
        assert plan.orphans_checked and len(plan.orphan_point_ids) == 1
        assert point_count(store) == 1, "a dry run deletes nothing"
        done = migrate.apply_migrate_imports(plan, prune_points=True, vector_store=store)
        assert done.pruned_points == 1 and point_count(store) == 0


def shutil_tree(src: Path, dst: Path) -> None:
    import shutil
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".quest-context"))


def test_mount_request_on_a_backend_that_cannot_mount_does_not_copy_import(tmp_path):
    """Mounting falls back to copy mode on the Quest backend; that must not revive the copy-import,
    which compounds imported ids (``root-a--root-b--...``) that ``card_origin.refuse`` then rejects."""
    from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
    repo = QuestApiCardRepository("https://quest.example.test", "k", "u1")
    store = FileContextStore(str(tmp_path / "cards"), card_repository=repo, mount_mode="mount",
                             repo_root=str(tmp_path))
    assert store._mount_mode == "copy"
    assert store._reuse_nested_cards is False
