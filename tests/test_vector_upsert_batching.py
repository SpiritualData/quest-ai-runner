"""Seeding a vector store must cost the same on 100 cards and 100,000.

``sync()`` hands every new or changed item to ``upsert``, which on a cold store is the entire
corpus. Embedding that in one call is what OOM-killed a runner mid-task: a transformer's attention
tensor is ``batch x heads x seq^2``, and the ONNX arena serving it grows to the high-water mark.
These tests pin the bound -- the embedder never sees more than one batch at a time -- without
needing a real model.
"""
import pytest

from quest_ai_runner.adapters import qdrant_vector_store as qvs


class FakeQdrant:
    def __init__(self):
        self.upserted = []

    def upsert(self, *, collection_name, points):
        self.upserted.extend(points)


def _store(embed_spy):
    """A QdrantVectorStore with qdrant and the embedder stubbed out (no server, no model)."""
    store = qvs.QdrantVectorStore.__new__(qvs.QdrantVectorStore)
    store._client = FakeQdrant()
    store._collection_name = lambda: "c"
    store._ensure_collection = lambda coll: None
    store._adopt_embedding_dim = lambda dim: None
    store._embed = embed_spy
    store._embed_safe = qvs.QdrantVectorStore._embed_safe.__get__(store)
    return store


def _items(n, text="x" * 50):
    return [{"id": f"c{i}", "text": text, "fingerprint": i, "payload": {}} for i in range(n)]


def test_the_embedder_never_sees_more_than_one_batch(monkeypatch):
    monkeypatch.setenv("QAR_EMBED_BATCH", "16")
    seen = []

    def embed(texts):
        seen.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    store = _store(embed)
    qvs.QdrantVectorStore.upsert(store, _items(100))

    assert max(seen) <= 16, f"a batch of {max(seen)} reached the embedder"
    assert sum(seen) == 100                      # every item still embedded, exactly once
    assert len(store._client.upserted) == 100    # and every point still written


def test_every_item_is_written_even_when_the_count_is_not_a_multiple_of_the_batch(monkeypatch):
    monkeypatch.setenv("QAR_EMBED_BATCH", "16")
    store = _store(lambda texts: [[0.0] for _ in texts])
    qvs.QdrantVectorStore.upsert(store, _items(37))   # 16 + 16 + 5
    assert len(store._client.upserted) == 37
    assert {p.payload["_id"] for p in store._client.upserted} == {f"c{i}" for i in range(37)}


def test_long_text_is_truncated_for_the_embedder_but_kept_whole_in_the_payload(monkeypatch):
    # The model truncates at its context window regardless, so sending more only costs tokenizer
    # memory. What SEARCH returns must be unaffected, so the payload keeps the full text.
    monkeypatch.setenv("QAR_EMBED_MAX_CHARS", "100")
    embedded = []

    def embed(texts):
        embedded.extend(texts)
        return [[0.0] for _ in texts]

    store = _store(embed)
    long_text = "y" * 5000
    qvs.QdrantVectorStore.upsert(store, [{"id": "c1", "text": long_text, "payload": {}}])

    assert len(embedded[0]) == 100
    assert store._client.upserted[0].payload["_text"] == long_text


def test_a_bad_batch_size_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("QAR_EMBED_BATCH", "not-a-number")
    assert qvs._embed_batch_size() == qvs._EMBED_BATCH_DEFAULT
    monkeypatch.setenv("QAR_EMBED_BATCH", "0")       # would make range() produce nothing
    assert qvs._embed_batch_size() == qvs._EMBED_BATCH_DEFAULT
    monkeypatch.setenv("QAR_EMBED_BATCH", "-5")
    assert qvs._embed_batch_size() == qvs._EMBED_BATCH_DEFAULT


def test_an_embedder_failure_stops_the_upsert_without_raising(monkeypatch):
    monkeypatch.setenv("QAR_EMBED_BATCH", "16")

    def embed(texts):
        raise RuntimeError("model unavailable")

    store = _store(embed)
    qvs.QdrantVectorStore.upsert(store, _items(50))   # must not raise
    assert store._client.upserted == []
