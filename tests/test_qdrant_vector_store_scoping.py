"""QdrantVectorStore single-collection payload scoping — behavior tests.

These run against the REAL embedded (local-filesystem) Qdrant engine with a
deterministic toy embedder, fully offline.  Skipped when qdrant-client is not
installed (same guard as the smoke test in test_vector_context.py).

The behaviors under test are the fix for collection sprawl: an earlier layout
created one Qdrant collection per unique scope dict — including on mere
search — which accumulated hundreds of permanently empty collections on a
shared server.  Now:

- ALL points live in one collection ({prefix}_default); scope is a payload
  filter.
- Read paths (search/count/evict_oldest) NEVER create a collection.
- Unscoped points are shared: visible to unscoped AND every scoped search.
- Scoped points are private to their exact scope.
- Same item id in two scopes does not collide.
- prune_scope_collections() deletes empty legacy per-scope collections.
"""
from __future__ import annotations

import pytest

pytest.importorskip("qdrant_client")

from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

VEC_SIZE = 8


def toy_embedder(texts):
    """Deterministic, dependency-free embedder: bucket characters into dims.

    Similar strings get similar vectors; exactness does not matter — the tests
    assert on membership/visibility, not on ranking.
    """
    vecs = []
    for t in texts:
        v = [0.0] * VEC_SIZE
        for i, ch in enumerate(t.encode()):
            v[i % VEC_SIZE] += (ch % 31) / 31.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        vecs.append([x / norm for x in v])
    return vecs


@pytest.fixture()
def store(tmp_path):
    return QdrantVectorStore(
        path=str(tmp_path / "qdrant"),
        embedder=toy_embedder,
        vector_size=VEC_SIZE,
    )


def collection_names(store):
    return {c.name for c in store._client.get_collections().collections}


def hit_ids(hits):
    return {h.id for h in hits}


class TestSingleCollection:
    def test_search_never_creates_collections(self, store):
        """A search under any novel scope must not create a collection (the sprawl bug)."""
        for i in range(5):
            assert store.search("anything", scope={"goal_id": f"g{i}"}) == []
        assert store.search("anything") == []
        assert collection_names(store) == set()

    def test_count_and_evict_never_create_collections(self, store):
        assert store.count(scope={"goal_id": "g1"}) == 0
        assert store.evict_oldest(3, scope={"goal_id": "g1"}) == 0
        assert collection_names(store) == set()

    def test_all_scopes_share_one_collection(self, store):
        store.upsert([{"id": "a", "text": "alpha"}], scope=None)
        store.upsert([{"id": "b", "text": "beta"}], scope={"team": "t1"})
        store.upsert([{"id": "c", "text": "gamma"}], scope={"team": "t2"})
        assert collection_names(store) == {f"qar_ctx_default_{VEC_SIZE}"}


class TestScopeVisibility:
    def test_scoped_search_sees_shared_and_own_scope_only(self, store):
        store.upsert([{"id": "shared1", "text": "shared corpus card"}], scope=None)
        store.upsert([{"id": "priv-a", "text": "private to team a"}], scope={"team": "a"})
        store.upsert([{"id": "priv-b", "text": "private to team b"}], scope={"team": "b"})

        ids_a = hit_ids(store.search("card", scope={"team": "a"}, top_k=10))
        assert "shared1" in ids_a          # shared data visible under a scope
        assert "priv-a" in ids_a           # own scoped data visible
        assert "priv-b" not in ids_a       # other scope's data invisible

    def test_unscoped_search_sees_only_shared(self, store):
        store.upsert([{"id": "shared1", "text": "shared corpus card"}], scope=None)
        store.upsert([{"id": "priv-a", "text": "private to team a"}], scope={"team": "a"})

        ids = hit_ids(store.search("card", top_k=10))
        assert ids == {"shared1"}

    def test_scope_payload_key_not_leaked_in_hit_payload(self, store):
        store.upsert([{"id": "p", "text": "point"}], scope={"team": "a"})
        (hit,) = store.search("point", scope={"team": "a"}, top_k=1)
        assert "_scope" not in hit.payload


class TestIdNamespacing:
    def test_same_item_id_in_two_scopes_does_not_collide(self, store):
        store.upsert([{"id": "assoc:x", "text": "team a version"}], scope={"team": "a"})
        store.upsert([{"id": "assoc:x", "text": "team b version"}], scope={"team": "b"})
        hits_a = store.search("version", scope={"team": "a"}, top_k=10)
        hits_b = store.search("version", scope={"team": "b"}, top_k=10)
        assert [h.text for h in hits_a] == ["team a version"]
        assert [h.text for h in hits_b] == ["team b version"]

    def test_sync_fingerprint_skip_is_per_scope(self, store):
        items = [{"id": "i1", "text": "hello", "fingerprint": "v1"}]
        assert store.sync(items, scope={"team": "a"}) == 1
        # Unchanged fingerprint in the SAME scope: nothing re-embedded.
        assert store.sync(items, scope={"team": "a"}) == 0
        # Same id + fingerprint in a DIFFERENT scope is a different point.
        assert store.sync(items, scope={"team": "b"}) == 1


class TestScopedAccounting:
    def test_count_is_exact_scope(self, store):
        store.upsert([{"id": "s", "text": "shared"}], scope=None)
        store.upsert(
            [{"id": "a1", "text": "one"}, {"id": "a2", "text": "two"}],
            scope={"team": "a"},
        )
        assert store.count(scope={"team": "a"}) == 2
        assert store.count() == 1

    def test_evict_oldest_only_touches_the_scope(self, store):
        store.upsert([{"id": "s", "text": "shared", "payload": {"ts": 1.0}}], scope=None)
        store.upsert(
            [
                {"id": "a1", "text": "old", "payload": {"ts": 10.0}},
                {"id": "a2", "text": "new", "payload": {"ts": 20.0}},
            ],
            scope={"team": "a"},
        )
        assert store.evict_oldest(1, scope={"team": "a"}) == 1
        assert store.count(scope={"team": "a"}) == 1
        # The shared point survives a scoped eviction.
        assert store.count() == 1
        remaining = hit_ids(store.search("old new", scope={"team": "a"}, top_k=10))
        assert "a1" not in remaining and "a2" in remaining


class TestPruneLegacyCollections:
    def test_prune_deletes_empty_legacy_scope_collections_only(self, store):
        from qdrant_client.models import Distance, VectorParams

        # Real data in the unified collection.
        store.upsert([{"id": "keep", "text": "kept point"}], scope=None)

        # Simulate the legacy sprawl: empty per-scope collections, one non-empty
        # legacy collection, and an unrelated collection owned by someone else.
        params = VectorParams(size=VEC_SIZE, distance=Distance.COSINE)
        client = store._client
        for suffix in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            client.create_collection(f"qar_ctx_{suffix}", vectors_config=params)
        client.create_collection("qar_ctx_dddddddddddd", vectors_config=params)
        from qdrant_client.models import PointStruct
        client.upsert(
            "qar_ctx_dddddddddddd",
            points=[PointStruct(id=1, vector=toy_embedder(["x"])[0], payload={})],
        )
        client.create_collection("someone_elses", vectors_config=params)

        assert store.prune_scope_collections() == 3
        names = collection_names(store)
        assert names == {
            f"qar_ctx_default_{VEC_SIZE}",
            "qar_ctx_dddddddddddd",
            "someone_elses",
        }
        # Idempotent.
        assert store.prune_scope_collections() == 0

    def test_prune_never_deletes_default_collections(self, store, tmp_path):
        from qdrant_client.models import Distance, VectorParams

        params = VectorParams(size=VEC_SIZE, distance=Distance.COSINE)
        client = store._client
        # Empty bare-legacy default and an empty size-keyed default of ANOTHER
        # embedder config must both survive a prune.
        client.create_collection("qar_ctx_default", vectors_config=params)
        client.create_collection("qar_ctx_default_1024", vectors_config=params)
        client.create_collection("qar_ctx_aaaaaaaaaaaa", vectors_config=params)
        assert store.prune_scope_collections() == 1
        assert collection_names(store) == {"qar_ctx_default", "qar_ctx_default_1024"}


class TestVectorSizeIsolation:
    """Two stores with different embedder dimensions on the same server must not
    collide on one collection (mixed sizes make Qdrant decline the mismatched
    writes point-by-point, silently under the never-raises contract)."""

    def test_different_vector_sizes_get_distinct_collections(self, tmp_path):
        path = str(tmp_path / "qdrant")

        def embed16(texts):
            return [[float((i + 1) % 7) for i in range(16)] for _ in texts]

        store_a = QdrantVectorStore(path=path, embedder=toy_embedder, vector_size=VEC_SIZE)
        store_a.upsert([{"id": "a", "text": "alpha item"}])
        assert [h.id for h in store_a.search("alpha item", top_k=5)] == ["a"]
        store_a._client.close()

        store_b = QdrantVectorStore(path=path, embedder=embed16, vector_size=16)
        # Must NOT be silently declined against store_a's 8-dim collection.
        store_b.upsert([{"id": "b", "text": "beta item"}])
        assert [h.id for h in store_b.search("beta item", top_k=5)] == ["b"]
        names = {c.name for c in store_b._client.get_collections().collections}
        assert names == {f"qar_ctx_default_{VEC_SIZE}", "qar_ctx_default_16"}
        store_b._client.close()

        # store_a's data is intact after store_b wrote.
        store_a2 = QdrantVectorStore(path=path, embedder=toy_embedder, vector_size=VEC_SIZE)
        assert [h.id for h in store_a2.search("alpha item", top_k=5)] == ["a"]

    def test_misdeclared_vector_size_adopts_real_embedding_dim(self, tmp_path):
        """The SD-prod regression shape: a store built with the DEFAULT
        vector_size (384) but a 1024-class embedder must not create a
        wrong-sized collection and then silently lose every write."""
        def embed16(texts):
            return [[1.0] * 16 for _ in texts]

        store = QdrantVectorStore(
            path=str(tmp_path / "qdrant"), embedder=embed16, vector_size=VEC_SIZE,
        )
        store.upsert([{"id": "x", "text": "real dim wins"}])
        assert [h.id for h in store.search("real dim wins", top_k=5)] == ["x"]
        names = {c.name for c in store._client.get_collections().collections}
        assert names == {"qar_ctx_default_16"}

    def test_matching_legacy_default_collection_is_reused(self, tmp_path):
        """Pre-size-keyed deployments keep their data: a bare {prefix}_default
        whose size matches the embedder is used as-is, no migration."""
        from qdrant_client.models import Distance, PointStruct, VectorParams

        path = str(tmp_path / "qdrant")
        seed = QdrantVectorStore(path=path, embedder=toy_embedder, vector_size=VEC_SIZE)
        client = seed._client
        client.create_collection(
            "qar_ctx_default",
            vectors_config=VectorParams(size=VEC_SIZE, distance=Distance.COSINE),
        )
        client.upsert(
            "qar_ctx_default",
            points=[PointStruct(
                id=1,
                vector=toy_embedder(["legacy point"])[0],
                payload={"_text": "legacy point"},
            )],
        )
        hits = seed.search("legacy point", top_k=5)
        assert len(hits) == 1 and hits[0].text == "legacy point"
        # New writes land in the same legacy collection (no split brain).
        seed.upsert([{"id": "n", "text": "new point"}])
        names = {c.name for c in client.get_collections().collections}
        assert names == {"qar_ctx_default"}

    def test_mismatched_legacy_default_collection_is_left_alone(self, tmp_path):
        """A bare {prefix}_default with a DIFFERENT size is not written to (its
        points would be declined); the store uses its size-keyed collection."""
        from qdrant_client.models import Distance, VectorParams

        path = str(tmp_path / "qdrant")
        store = QdrantVectorStore(path=path, embedder=toy_embedder, vector_size=VEC_SIZE)
        store._client.create_collection(
            "qar_ctx_default",
            vectors_config=VectorParams(size=999, distance=Distance.COSINE),
        )
        store.upsert([{"id": "k", "text": "kept safe"}])
        assert [h.id for h in store.search("kept safe", top_k=5)] == ["k"]
        names = {c.name for c in store._client.get_collections().collections}
        assert names == {"qar_ctx_default", f"qar_ctx_default_{VEC_SIZE}"}
