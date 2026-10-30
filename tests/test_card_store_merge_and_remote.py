"""Merging a store into a wider store, and read-only vectors for a mount on another Qdrant backend.

Offline: tmp dirs, embedded Qdrant, a counting fake embedder. No network, no model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

pytest.importorskip("qdrant_client")

from quest_ai_runner import store_cli  # noqa: E402
from hubofhubs.recall import card_store_identity as ident  # noqa: E402
from hubofhubs.recall import card_store_merge as hub_merge  # noqa: E402
from quest_ai_runner.adapters import card_store_merge as merge  # noqa: E402
from hubofhubs.recall import card_store_mounts as mounts
from quest_ai_runner.adapters import card_store_fence as fence  # noqa: E402
from quest_ai_runner.adapters import card_store_vectors as vectors  # noqa: E402
from hubofhubs.recall.card_repository import FilesystemCardRepository  # noqa: E402
from quest_ai_runner.adapters.file_context_store import FileContextStore  # noqa: E402
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore  # noqa: E402
from quest_ai_runner.core.vector_scopes import CARD_SEED_SCOPE  # noqa: E402
from tests.test_card_store_mounts import (  # noqa: E402
    VEC,
    CountingEmbedder,
    card,
    make_owner,
    point_count,
    reader_store,
    snapshot,
)

EMBEDDER = "test:embedder-a"
SERVER_URL = "http://qdrant.example.test:6333"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def real_card(card_id: str, root: Path, rel_files: List[str], summary: str = "") -> Dict[str, Any]:
    """A card whose file entries carry the real sha256 and mtime, so freshness checks mean something."""
    entries = []
    for rel in rel_files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {card_id} {rel}\ndef f():\n    pass\n", encoding="utf-8")
        entries.append({"path": rel, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "mtime": target.stat().st_mtime})
    c = card(card_id, [], summary=summary or f"About {card_id}")
    c["files"] = entries
    return c


def make_store(root: Path, cards: List[Dict[str, Any]], *, legacy: bool = False) -> Path:
    """A bootstrapped store under ``root`` (files already on disk). ``legacy`` = a store that
    predates identity, then given one: its points use the unqualified ids."""
    cards_dir = root / ".quest-context"
    cards_dir.mkdir(parents=True, exist_ok=True)
    repo = FilesystemCardRepository(str(cards_dir))
    for c in cards:
        repo.write(c["id"], c)
    make_owner_meta(cards_dir, root, len(cards), legacy)
    return cards_dir


def make_owner_meta(cards_dir: Path, root: Path, count: int, legacy: bool) -> None:
    from quest_ai_runner.adapters.file_context_store import _write_bootstrap_meta
    if legacy:
        ident.write_meta(str(cards_dir), {"version": 1, "feature_versions": {}, "card_count": count,
                                          "root": str(root)})
        assert ident.ensure_store_id(str(cards_dir))
        assert ident.read_meta(str(cards_dir))["vector_id_scheme"] == "legacy"
    else:
        _write_bootstrap_meta(str(cards_dir), count, root=str(root))
        assert ident.read_meta(str(cards_dir))["vector_id_scheme"] == "qualified"


def store_over(cards_dir: Path, root: Path, **kw) -> FileContextStore:
    return FileContextStore(str(cards_dir), repo_root=str(root), auto_bootstrap=False, **kw)


def cards_in(cards_dir: Path) -> Dict[str, Dict[str, Any]]:
    return FilesystemCardRepository(str(cards_dir)).load_all()


@pytest.fixture()
def qdrant(tmp_path):
    emb = CountingEmbedder()
    return QdrantVectorStore(path=str(tmp_path / "q"), embedder=emb, vector_size=VEC,
                             embedder_id=EMBEDDER), emb


def seed(vs: QdrantVectorStore, store: FileContextStore) -> int:
    return vs.sync(store.export_for_embedding(), scope=CARD_SEED_SCOPE)


def seeded_points(vs: QdrantVectorStore) -> Dict[str, Dict[str, Any]]:
    """Every card point as {item id: payload}."""
    out = {}
    client = vs._client
    coll = vs._collection_name()
    points, _ = client.scroll(collection_name=coll, limit=1000, with_payload=True)
    for p in points:
        payload = dict(p.payload or {})
        out[payload.get("_id")] = payload
    return out


# --------------------------------------------------------------------------------------------
# merge: cards, state, identity
# --------------------------------------------------------------------------------------------

class TestMergeCards:
    def _pair(self, tmp_path, *, wide_cards=None, legacy=False):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py", "pkg/b.py"]),
                                          real_card("beta", narrow_root, ["c.py"])], legacy=legacy)
        wide = make_store(top, wide_cards or [real_card("gamma", top, ["g.py"])], legacy=legacy)
        return top, narrow, wide

    def test_dry_run_reports_counts_and_writes_nothing(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        before = (snapshot(narrow), snapshot(wide))
        plan = merge.plan_merge(str(narrow), str(wide))
        assert plan.ok and plan.prefix == "sub"
        assert plan.count("move") == 2 and plan.count("renamed", "unresolved") == 0
        assert plan.files_missing == 0
        text = plan.render()
        assert "cards to move: 2" in text and "dry run" in text and "re-embeds: 0" in text
        assert (snapshot(narrow), snapshot(wide)) == before

    def test_apply_moves_cards_with_ids_unchanged_and_paths_prefixed(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        before_narrow = snapshot(narrow)
        sid = ident.store_id_of(str(narrow))
        plan = merge.apply_merge(str(narrow), str(wide))
        assert plan.applied and not plan.source_pruned
        cards = cards_in(wide)
        assert set(cards) == {"alpha", "beta", "gamma"}
        assert [fe["path"] for fe in cards["alpha"]["files"]] == ["sub/a.py", "sub/pkg/b.py"]
        assert cards["alpha"]["provenance"]["merged_from"] == {"store_id": sid, "card_id": "alpha"}
        assert cards["gamma"]["files"][0]["path"] == "g.py", "the wide store's own cards are untouched"
        # without --prune-source the source is exactly as it was
        assert snapshot(narrow) == before_narrow
        assert "left untouched" in plan.render()
        meta = ident.read_meta(str(wide))
        assert meta["merged_from"][0]["store_id"] == sid and meta["merged_from"][0]["merged_at"]
        assert meta["merged_from"][0]["cards"] == 2 and meta["card_count"] == 3

    def test_turns_and_reps_move_too_and_never_clobber(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        (narrow / "turns").mkdir()
        (narrow / "turns" / "t1.json").write_text('{"n": 1}')
        (narrow / "turns" / "t2.json").write_text('{"n": "narrow"}')
        (narrow / "reps" / "u1" / "notes").mkdir(parents=True)
        (narrow / "reps" / "u1" / "notes" / "n.json").write_text('{"r": 1}')
        (narrow / "qdrant").mkdir()
        (narrow / "qdrant" / "meta.json").write_text("{}")
        (wide / "turns").mkdir()
        (wide / "turns" / "t2.json").write_text('{"n": "wide"}')
        plan = merge.apply_merge(str(narrow), str(wide), prune_source_after=True)
        assert plan.applied
        assert (wide / "turns" / "t1.json").read_text() == '{"n": 1}'
        assert (wide / "reps" / "u1" / "notes" / "n.json").exists()
        assert (wide / "turns" / "t2.json").read_text() == '{"n": "wide"}', "no clobber"
        assert plan.state_collisions == ["turns/t2.json"]
        assert not (wide / "qdrant").exists(), "vector directories are not state to move"
        assert (narrow / "turns" / "t2.json").exists(), "a collided file stays in the source"
        assert not (narrow / "turns" / "t1.json").exists()

    def test_id_collision_keeps_the_wide_card_renames_the_incoming_and_rerun_is_a_noop(self, tmp_path):
        top = tmp_path / "x"
        wide_alpha = real_card("alpha", top, ["other/alpha.py"], summary="wide alpha")
        top_, narrow, wide = self._pair(tmp_path, wide_cards=[wide_alpha])
        sid = ident.store_id_of(str(narrow))
        plan = merge.plan_merge(str(narrow), str(wide))
        assert plan.count("renamed") == 1 and "collision alpha: renamed" in plan.render()
        renamed = hub_merge.renamed_id("alpha", sid)
        merge.apply_merge(str(narrow), str(wide))
        cards = cards_in(wide)
        assert cards["alpha"]["summary"] == "wide alpha", "the wide card is never overwritten"
        assert [fe["path"] for fe in cards[renamed]["files"]] == ["sub/a.py", "sub/pkg/b.py"]
        assert cards[renamed]["id"] == renamed
        after_first = snapshot(wide)
        again = merge.apply_merge(str(narrow), str(wide))
        assert again.applied and again.count("move", "renamed") == 0 and again.count("already") == 2
        assert snapshot(wide) == after_first, "re-running changes nothing"

    def test_a_wide_card_with_the_same_name_and_files_is_the_same_card(self, tmp_path):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"])])
        twin = real_card("alpha", top, ["sub/a.py"])
        wide = make_store(top, [twin])
        plan = merge.plan_merge(str(narrow), str(wide))
        assert plan.count("already") == 1 and plan.count("renamed") == 0

    def test_prune_source_removes_moved_cards_marks_it_merged_and_discovery_ignores_it(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        wide_sid = ident.store_id_of(str(wide))
        plan = merge.apply_merge(str(narrow), str(wide), prune_source_after=True)
        assert plan.source_pruned
        assert cards_in(narrow) == {}
        meta = ident.read_meta(str(narrow))
        assert meta["merged_into"]["store_id"] == wide_sid and meta["store_id"]
        assert ident.is_merged(str(narrow))
        assert mounts.claims_subtree(top / "sub", ".quest-context") is False
        assert mounts.claims_subtree(top, ".quest-context") is True
        # a reader above both sees the wide store only
        reader_root = top.parent
        found = mounts.discover_nested_stores(reader_root, reader_root / ".quest-context", set(), store_dir_name=".quest-context")
        assert found == [top]
        assert ident.check_readable(str(narrow)).allowed is False
        again = merge.apply_merge(str(narrow), str(wide), prune_source_after=True)
        assert again.applied and again.problems == []
        assert ident.read_meta(str(wide))["merged_from"][0]["cards"] == 2

    def test_copy_mode_discovery_also_treats_a_merged_store_as_not_a_store(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        probe = store_over(tmp_path / "probe-cards", top)
        assert probe._discover_nested_card_dirs(top, set()) == [top / "sub"]
        merge.apply_merge(str(narrow), str(wide), prune_source_after=True)
        assert probe._discover_nested_card_dirs(top, set()) == []

    def test_copy_imports_of_the_source_in_the_wide_store_are_replaced_by_the_moved_cards(self, tmp_path):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"])])
        wide = top / ".quest-context"
        copier = store_over(wide, top, mount_mode="copy")
        copier.bootstrap(root=str(top))
        copies = [cid for cid in cards_in(wide) if "--" in cid]
        assert len(copies) == 1
        plan = merge.plan_merge(str(narrow), str(wide))
        assert plan.wide_copies == copies
        done = merge.apply_merge(str(narrow), str(wide), prune_source_after=True)
        assert done.wide_copies_removed == 1
        assert sorted(cards_in(wide)) == ["alpha"]

    def test_a_missing_wide_store_is_created_with_an_identity(self, tmp_path):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"])])
        wide = top / ".quest-context"
        plan = merge.apply_merge(str(narrow), str(wide))
        assert plan.applied
        meta = ident.read_meta(str(wide))
        assert meta["store_id"] and meta["root"] == str(top) and meta["vector_id_scheme"] == "qualified"
        assert set(cards_in(wide)) == {"alpha"}

    def test_refuses_a_source_outside_the_target_and_a_source_without_identity(self, tmp_path):
        a = make_store(tmp_path / "a", [real_card("c", tmp_path / "a", ["f.py"])])
        b = make_store(tmp_path / "b", [real_card("d", tmp_path / "b", ["g.py"])])
        assert any("not inside" in p for p in merge.plan_merge(str(a), str(b)).problems)
        assert merge.apply_merge(str(a), str(b)).applied is False
        assert any("same store" in p for p in merge.plan_merge(str(a), str(a)).problems)

    def test_missing_files_are_counted(self, tmp_path):
        top, narrow, wide = self._pair(tmp_path)
        (top / "sub" / "c.py").unlink()
        plan = merge.plan_merge(str(narrow), str(wide))
        assert plan.files_missing == 1 and plan.missing_samples == ["sub/c.py"]


class TestMergeFreshness:
    def test_the_wide_stores_normal_refresh_treats_moved_cards_as_its_own(self, tmp_path):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        provider = MagicMock()
        provider.list_models.return_value = []
        provider.answer.return_value = "[]"
        # Each store's own refresh has run once (a fresh card is normalised on its first pass), so
        # what the wide store does after the merge is attributable to the merge alone.
        wide = make_store(top, [real_card("gamma", top, ["g.py"])])
        store_over(wide, top).refresh_stale(root=str(top), provider=provider)
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"]),
                                          real_card("beta", narrow_root, ["pkg/b.py"])])
        store_over(narrow, narrow_root).refresh_stale(root=str(narrow_root), provider=provider)
        merge.apply_merge(str(narrow), str(wide), prune_source_after=True)

        store = store_over(wide, top)
        before = snapshot(wide)
        assert store.stale_cards_for("sub/a.py") == set(), "fingerprints match at the new prefixed path"
        assert store.refresh_stale(root=str(top), provider=provider) == 0
        assert provider.answer.call_count == 0, "no card is re-made for an unchanged file"
        assert {k: v for k, v in snapshot(wide).items() if "bootstrap" not in k} \
            == {k: v for k, v in before.items() if "bootstrap" not in k}, "no card was rewritten"

        (narrow_root / "a.py").write_text("changed = True\n", encoding="utf-8")
        assert store.stale_cards_for("sub/a.py") == {"alpha"}, "a changed file is stale under the new path"


# --------------------------------------------------------------------------------------------
# merge: vectors without re-embedding
# --------------------------------------------------------------------------------------------

class TestMergeVectors:
    def _setup(self, tmp_path, qdrant, *, legacy):
        vs, emb = qdrant
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"], "alpha billing"),
                                          real_card("beta", narrow_root, ["pkg/b.py"], "beta invoices")],
                            legacy=legacy)
        wide = make_store(top, [real_card("gamma", top, ["g.py"], "gamma notes")], legacy=legacy)
        assert seed(vs, store_over(narrow, narrow_root)) == 2
        assert seed(vs, store_over(wide, top)) == 1
        return top, narrow, wide, vs, emb

    def test_legacy_scheme_points_keep_their_ids_and_only_the_label_changes(self, tmp_path, qdrant):
        top, narrow, wide, vs, emb = self._setup(tmp_path, qdrant, legacy=True)
        spent, points = emb.texts, point_count(vs)
        wide_sid = ident.store_id_of(str(wide))
        dry = merge.plan_merge(str(narrow), str(wide), vector_store=vs)
        assert dry.points["relabelled"] == 2 and dry.points["copied"] == 0
        assert seeded_points(vs)["card:alpha"]["store_id"] == ident.store_id_of(str(narrow)), \
            "a dry run relabels nothing"

        done = merge.apply_merge(str(narrow), str(wide), vector_store=vs)
        assert done.points["relabelled"] == 2 and done.points["copied"] == 0
        assert emb.texts == spent, "the embedder was called 0 times"
        assert point_count(vs) == points
        pts = seeded_points(vs)
        assert pts["card:alpha"]["store_id"] == wide_sid and pts["card:alpha"]["paths"] == ["sub/a.py"]
        assert pts["card:beta"]["paths"] == ["sub/pkg/b.py"]
        # the wide lane's seed pass finds every moved card already embedded
        assert seed(vs, store_over(wide, top)) == 0
        assert emb.texts == spent

    def test_qualified_scheme_points_are_copied_with_their_stored_vector_then_the_old_ones_go(self, tmp_path, qdrant):
        top, narrow, wide, vs, emb = self._setup(tmp_path, qdrant, legacy=False)
        spent, points = emb.texts, point_count(vs)
        narrow_sid, wide_sid = ident.store_id_of(str(narrow)), ident.store_id_of(str(wide))
        done = merge.apply_merge(str(narrow), str(wide), vector_store=vs)
        assert done.points["copied"] == 2 and done.points["relabelled"] == 0
        assert emb.texts == spent, "the embedder was called 0 times"
        pts = seeded_points(vs)
        assert f"card:{wide_sid}:alpha" in pts and f"card:{narrow_sid}:alpha" in pts, \
            "without --prune-source the source's points stay"
        assert point_count(vs) == points + 2
        assert seed(vs, store_over(wide, top)) == 0 and emb.texts == spent

        pruned = merge.apply_merge(str(narrow), str(wide), vector_store=vs, prune_source_after=True)
        assert pruned.applied
        pts = seeded_points(vs)
        assert f"card:{narrow_sid}:alpha" not in pts and f"card:{wide_sid}:alpha" in pts
        assert point_count(vs) == points
        assert pts[f"card:{wide_sid}:alpha"]["paths"] == ["sub/a.py"]
        assert emb.texts == spent

    def test_the_moved_vector_is_the_stored_one(self, tmp_path, qdrant):
        top, narrow, wide, vs, emb = self._setup(tmp_path, qdrant, legacy=False)
        narrow_sid, wide_sid = ident.store_id_of(str(narrow)), ident.store_id_of(str(wide))
        before = vs.search("alpha billing", scope=CARD_SEED_SCOPE, top_k=10)
        top_before = next(h for h in before if h.id == f"card:{narrow_sid}:alpha")
        merge.apply_merge(str(narrow), str(wide), vector_store=vs, prune_source_after=True)
        after = vs.search("alpha billing", scope=CARD_SEED_SCOPE, top_k=10)
        moved = next(h for h in after if h.id == f"card:{wide_sid}:alpha")
        assert moved.score == pytest.approx(top_before.score)

    def test_rerun_with_vectors_is_idempotent(self, tmp_path, qdrant):
        top, narrow, wide, vs, emb = self._setup(tmp_path, qdrant, legacy=False)
        merge.apply_merge(str(narrow), str(wide), vector_store=vs, prune_source_after=True)
        spent, points, snap = emb.texts, point_count(vs), seeded_points(vs)
        again = merge.apply_merge(str(narrow), str(wide), vector_store=vs, prune_source_after=True)
        assert again.applied and emb.texts == spent and point_count(vs) == points
        assert seeded_points(vs) == snap

    def test_a_renamed_legacy_card_does_not_borrow_the_wide_cards_point(self, tmp_path, qdrant):
        vs, emb = qdrant
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"], "narrow alpha")],
                            legacy=True)
        wide = make_store(top, [real_card("alpha", top, ["other.py"], "wide alpha")], legacy=True)
        assert seed(vs, store_over(wide, top)) == 1
        spent = emb.texts
        done = merge.apply_merge(str(narrow), str(wide), vector_store=vs, prune_source_after=True)
        assert done.applied and emb.texts == spent
        assert seeded_points(vs)["card:alpha"]["paths"] == ["other.py"], "the wide card's point is intact"


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

class TestMergeCli:
    def test_merge_into_is_a_dry_run_by_default(self, tmp_path, capsys):
        top = tmp_path / "x"
        narrow_root = top / "sub"
        narrow = make_store(narrow_root, [real_card("alpha", narrow_root, ["a.py"])])
        wide = make_store(top, [real_card("gamma", top, ["g.py"])])
        before = (snapshot(narrow), snapshot(wide))
        assert store_cli.is_store_command(["context", "merge-into"])
        assert store_cli.run(["context", "merge-into", "--cards-dir", str(narrow), "--into", str(wide)]) == 0
        assert "dry run" in capsys.readouterr().out
        assert (snapshot(narrow), snapshot(wide)) == before
        assert store_cli.run(["context", "merge-into", "--cards-dir", str(narrow), "--into", str(wide),
                              "--apply", "--prune-source"]) == 0
        assert "APPLIED" in capsys.readouterr().out
        assert ident.is_merged(str(narrow)) and "alpha" in cards_in(wide)


# --------------------------------------------------------------------------------------------
# vector location, set by the owner
# --------------------------------------------------------------------------------------------

class TestVectorLocationIdentity:
    def test_store_info_records_the_location_only_with_apply(self, tmp_path, capsys):
        d = make_owner(tmp_path / "o", [card("c", ["a.py"])])
        args = ["context", "store-info", "--cards-dir", str(d), "--vector-location", SERVER_URL,
                "--vector-size", "1024", "--embedder-id", "voyage:test-model"]
        assert store_cli.run(args) == 0
        assert ident.read_meta(str(d)).get("vector_location") is None
        assert "dry run" in capsys.readouterr().out
        assert store_cli.run(args + ["--apply"]) == 0
        rec = ident.vector_location_of(ident.read_meta(str(d)))
        assert rec == {"location": SERVER_URL, "collection_prefix": "qar_ctx", "vector_size": 1024,
                       "embedder": "voyage:test-model"}

    def test_embedded_records_the_directory_and_survives_a_bootstrap_rewrite(self, tmp_path):
        d = make_owner(tmp_path / "o", [card("c", ["a.py"])])
        assert store_cli.run(["context", "store-info", "--cards-dir", str(d), "--vector-location",
                              "embedded", "--apply"]) == 0
        rec = ident.vector_location_of(ident.read_meta(str(d)))
        assert rec["location"] == "embedded" and rec["path"] == str(d / "qdrant")
        from quest_ai_runner.adapters.file_context_store import _write_bootstrap_meta
        _write_bootstrap_meta(str(d), 1, root=str(tmp_path / "o"))
        assert ident.vector_location_of(ident.read_meta(str(d))) == rec

    def test_compatibility_rule(self):
        rec = {"vector_size": VEC, "embedder": EMBEDDER}
        assert vectors.compatibility(rec, query_dim=VEC, reader_embedder=EMBEDDER).ok
        assert vectors.compatibility(rec, query_dim=VEC, reader_embedder="").ok, "unnamed side: by size"
        assert not vectors.compatibility(rec, query_dim=VEC + 1, reader_embedder=EMBEDDER).ok
        assert not vectors.compatibility(rec, query_dim=VEC, reader_embedder="other:model").ok
        assert not vectors.compatibility(rec, query_dim=None).ok


# --------------------------------------------------------------------------------------------
# a reader with a private embedded Qdrant, a mount whose points are on another backend
# --------------------------------------------------------------------------------------------

class RecordingClient:
    """Wraps the 'server' client and records every call, so a test can prove nothing was written."""
    READS = frozenset({"collection_exists", "get_collection", "get_collections", "query_points"})

    def __init__(self, inner, fail: bool = False):
        self.inner, self.calls, self.fail = inner, [], fail

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self.calls.append(name)
            if self.fail:
                raise ConnectionError("unreachable")
            return attr(*a, **k)
        return call

    @property
    def writes(self) -> List[str]:
        return [c for c in self.calls if c not in self.READS]


class RemoteSetup:
    def __init__(self, tmp_path: Path, *, reader_embedder_id: str = EMBEDDER, record_embedder: str = EMBEDDER,
                 record_size: int = VEC, fail: bool = False, location: str = SERVER_URL,
                 with_record: bool = True):
        self.tmp = tmp_path
        self.wide = tmp_path / "wide"
        self.owner_dir = make_owner(self.wide / "proj", [card("alpha", ["sub/a.py"], summary="alpha billing"),
                                                         card("beta", ["sub/b.py"], summary="beta invoices")])
        self.owner = store_over(self.owner_dir, self.wide / "proj")
        self.server_emb = CountingEmbedder()
        self.server = QdrantVectorStore(path=str(tmp_path / "server"), embedder=self.server_emb,
                                        vector_size=VEC, embedder_id=EMBEDDER)
        assert seed(self.server, self.owner) == 2
        self.server_points = point_count(self.server)
        self.sid = ident.store_id_of(str(self.owner_dir))
        if with_record:
            ident.update_identity(str(self.owner_dir), vector_location=ident.vector_location_record(
                location, collection_prefix="qar_ctx", vector_size=record_size, embedder=record_embedder))
        self.reader_emb = CountingEmbedder()
        self.private = QdrantVectorStore(path=str(tmp_path / "private"), embedder=self.reader_emb,
                                         vector_size=VEC, embedder_id=reader_embedder_id)
        self.client = RecordingClient(self.server._client, fail=fail)
        self.opened: List[Any] = []
        self.reader = reader_store(self.wide)
        self.reader.bootstrap(root=str(self.wide))
        self.fenced = fence.StoreFencedVectorStore(self.private, self.reader, reader_factory=self.factory)

    def factory(self, location, prefix):
        self.opened.append((location, prefix))
        return vectors.ReadOnlyVectorReader(self.client, collection_prefix=prefix)


class TestRemoteMountVectors:
    def test_reader_queries_the_mounted_points_on_the_other_backend_and_writes_nothing(self, tmp_path):
        s = RemoteSetup(tmp_path)
        before = snapshot(s.owner_dir)
        hits = s.fenced.search("alpha billing", top_k=5)
        assert {h.id for h in hits} == {f"card:{s.sid}:alpha", f"card:{s.sid}:beta"}
        alpha = next(h for h in hits if h.id.endswith(":alpha"))
        assert alpha.payload["paths"] == ["proj/sub/a.py"], "paths are rebased to the reader's root"
        assert s.opened == [(SERVER_URL, "qar_ctx")]
        # zero writes to the mounted location, structurally and as observed
        assert s.client.calls and s.client.writes == []
        assert not hasattr(vectors.ReadOnlyVectorReader, "upsert")
        assert not any(hasattr(vectors.ReadOnlyVectorReader, n)
                       for n in ("delete", "set_payload", "create_collection", "sync", "evict_oldest"))
        assert point_count(s.server) == s.server_points
        assert snapshot(s.owner_dir) == before
        # nothing was copied into, or embedded for, the reader's private store
        assert point_count(s.private) == 0
        assert s.reader_emb.texts == 1, "only the query was embedded, nothing from the mount"
        assert s.server_emb.texts == 2

    def test_the_readers_own_writes_never_reach_the_mounted_location(self, tmp_path):
        s = RemoteSetup(tmp_path)
        own = s.reader.own_store_id()
        s.fenced.sync([{"id": "card:own", "text": "own card text", "fingerprint": "f",
                        "payload": {"store_id": own}}], scope=CARD_SEED_SCOPE)
        s.fenced.upsert([{"id": "assoc:1", "text": "an association", "payload": {}}])
        s.fenced.search("anything", top_k=3)
        assert s.client.writes == []
        assert point_count(s.server) == s.server_points
        assert point_count(s.private) == 2

    def test_own_and_remote_hits_share_one_ranking(self, tmp_path):
        s = RemoteSetup(tmp_path)
        own_card = card("mine", ["m.py"], summary="my own billing notes")
        FilesystemCardRepository(str(s.wide / ".quest-context")).write("mine", own_card)
        s.reader = reader_store(s.wide)
        s.fenced = fence.StoreFencedVectorStore(s.private, s.reader, reader_factory=s.factory)
        assert seed(s.private, s.reader) == 1
        hits = s.fenced.search("alpha billing", top_k=5)
        assert len(hits) == 3
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
        assert {h.id for h in hits} == {f"card:{s.reader.own_store_id()}:mine",
                                        f"card:{s.sid}:alpha", f"card:{s.sid}:beta"}

    def test_incompatible_embedder_family_skips_that_mounts_vector_arm_and_logs(self, tmp_path, caplog):
        s = RemoteSetup(tmp_path, reader_embedder_id="test:embedder-b")
        with caplog.at_level(logging.WARNING):
            assert s.fenced.search("alpha billing", top_k=5) == []
        assert "vector arm skipped" in caplog.text and "test:embedder-b" in caplog.text
        assert s.opened == [] and s.client.calls == []
        # the keyword arm still serves the mounted card
        result = s.reader.assemble("alpha billing", meta={})
        assert result.card_ids and result.card_ids[0].endswith(":alpha")

    def test_dimension_mismatch_skips_that_mounts_vector_arm(self, tmp_path, caplog):
        s = RemoteSetup(tmp_path, record_size=VEC * 2)
        with caplog.at_level(logging.WARNING):
            assert s.fenced.search("alpha billing", top_k=5) == []
        assert "dimensional" in caplog.text and s.client.calls == []

    def test_an_unreachable_server_is_logged_once_and_the_keyword_arm_still_works(self, tmp_path, caplog):
        s = RemoteSetup(tmp_path, fail=True)
        with caplog.at_level(logging.WARNING):
            assert s.fenced.search("alpha billing", top_k=5) == []
            first_calls = len(s.client.calls)
            assert s.fenced.search("alpha billing", top_k=5) == []
        assert caplog.text.count("is unreachable") == 1
        assert len(s.client.calls) == first_calls, "a down location is not retried on every turn"
        assert s.reader.assemble("alpha billing", meta={}).card_ids
        assert s.client.writes == []

    def test_an_embedded_location_elsewhere_is_never_opened(self, tmp_path, caplog):
        s = RemoteSetup(tmp_path, with_record=False)
        ident.update_identity(str(s.owner_dir), vector_location=ident.vector_location_record(
            "embedded", collection_prefix="qar_ctx", vector_size=VEC, embedder=EMBEDDER) | {"path": str(tmp_path / "somewhere" / "qdrant")})
        with caplog.at_level(logging.WARNING):
            assert s.fenced.search("alpha billing", top_k=5) == []
        assert "embedded Qdrant" in caplog.text
        assert s.opened == [] and s.client.calls == []
        assert s.reader.assemble("alpha billing", meta={}).card_ids

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
    def test_an_embedded_location_in_an_unreadable_directory_says_so(self, tmp_path, caplog):
        s = RemoteSetup(tmp_path, with_record=False)
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            ident.update_identity(str(s.owner_dir), vector_location={
                "location": "embedded", "path": str(locked), "vector_size": VEC})
            with caplog.at_level(logging.WARNING):
                assert s.fenced.search("alpha billing", top_k=5) == []
            assert "directory this user cannot read" in caplog.text
        finally:
            os.chmod(locked, 0o755)

    def test_a_mount_with_no_recorded_location_or_the_same_backend_is_unchanged(self, tmp_path):
        s = RemoteSetup(tmp_path, with_record=False)
        # no vector_location: queried through the reader's own store with the fence, as before
        mount = s.reader.live_mounts()[0][0]
        assert s.fenced.classify(mount) == "same"
        assert s.fenced.search("alpha billing", top_k=5) == []   # nothing of it in the private store
        assert s.opened == [] and s.client.calls == []
        # a record naming the reader's own backend and prefix is also served the existing way
        mount.vector_location = {"location": "embedded", "collection_prefix": "qar_ctx"}
        assert s.fenced.classify(mount) == "same", "embedded with no directory recorded: as before"
        s.private.location = SERVER_URL
        mount.vector_location = {"location": SERVER_URL, "collection_prefix": "qar_ctx"}
        assert s.fenced.classify(mount) == "same"
        mount.vector_location = {"location": SERVER_URL, "collection_prefix": "other_prefix"}
        assert s.fenced.classify(mount) == "remote"


class TestDefaultsUnchanged:
    def test_default_mode_is_copy_and_no_mount_machinery_is_involved(self, tmp_path):
        store = FileContextStore(str(tmp_path / "c"), repo_root=str(tmp_path), auto_bootstrap=False)
        assert store.mounts_enabled is False and store.live_mounts() == []

    def test_identity_value_additions_do_not_change_what_a_copy_mode_store_exports(self, tmp_path):
        top = tmp_path / "x"
        d = make_store(top, [real_card("alpha", top, ["a.py"])])
        before = store_over(d, top).export_for_embedding()
        ident.update_identity(str(d), vector_location=ident.vector_location_record(SERVER_URL, collection_prefix="qar_ctx"))
        assert store_over(d, top).export_for_embedding() == before

    def test_plain_vector_search_signature_still_works_without_a_fence(self, tmp_path, qdrant):
        vs, emb = qdrant
        top = tmp_path / "x"
        d = make_store(top, [real_card("alpha", top, ["a.py"], "alpha billing")])
        assert seed(vs, store_over(d, top)) == 1
        assert [h.id for h in vs.search("alpha billing", top_k=3, scope=CARD_SEED_SCOPE)]
