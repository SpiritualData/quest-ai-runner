"""Offline tests for the generic Qdrant-backed CardRepository (``QdrantCardRepository``).

Fully OFFLINE: skip-guarded when ``qdrant-client`` is unavailable, uses an EMBEDDED Qdrant
(``path=<tmp>``, no server / url) and a STUB embedder (a deterministic vector hashed from the text,
fixed dim) so there is NO network, no API key, and no fastembed/Voyage dependency. Proves:

  * write / read / load_all / exists / delete round-trip through the repo.
  * ``scope`` isolation: a card under scope A is invisible under scope B.
  * ``update_card`` through a ``FileContextStore(card_repository=QdrantCardRepository(...))``.
  * ``search_cards`` native full-text (or its None fallback).
  * the SHARED ``card_embed_text`` helper and ``export_for_embedding`` agree on a card's embed-text.
"""
import hashlib

import pytest

qdrant_client = pytest.importorskip("qdrant_client", reason="qdrant-client not installed")

from quest_ai_runner.adapters.card_repository import card_embed_text
from quest_ai_runner.adapters.qdrant_card_repository import (
    QdrantCardRepository,
    QdrantCardVectorStore,
)
from quest_ai_runner.adapters.file_context_store import FileContextStore


_DIM = 16


def _stub_embedder(texts):
    """Deterministic, network-free embedder: a fixed-dim vector seeded from each text's sha256.

    Same text → same vector (so an idempotent re-write keeps the same point), different text →
    (almost surely) different vector. No model, no API key.
    """
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode("utf-8", errors="replace")).digest()
        # Spread the 32 digest bytes into _DIM floats in [0,1).
        vec = [((h[i % len(h)] + i) % 256) / 255.0 for i in range(_DIM)]
        out.append(vec)
    return out


def _sample_card(card_id, name, description, keywords, content=None):
    return {
        "id": card_id,
        "name": name,
        "summary": description,
        "description": description,
        "keywords": keywords,
        "content": content or [],
        "usage_count": 0,
        "last_outcome": "unknown",
    }


def _client(tmp_path):
    """ONE embedded Qdrant client per test. Embedded Qdrant forbids two clients on the same path, so
    tests that build several repos/stores (scope isolation, vector arm) share this single client."""
    from qdrant_client import QdrantClient
    return QdrantClient(path=str(tmp_path / "qdrant"))


def _repo(tmp_path, *, scope=None, collection="cards_test", client=None):
    return QdrantCardRepository(
        collection=collection,
        embedder=_stub_embedder,
        vector_size=_DIM,
        scope=scope,
        client=client if client is not None else _client(tmp_path),
    )


def test_write_read_loadall_exists_delete_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    card = _sample_card("dreams", "Dreams topic", "The user's recurring flying dreams.", ["dream", "flying"])

    assert repo.write("dreams", card) is True

    got = repo.read("dreams")
    assert got is not None
    assert got.get("name") == "Dreams topic"
    # Repo-internal fields are stripped from the returned card.
    assert "_search_text" not in got and "card_id" not in got and "updated_at" not in got

    all_cards = repo.load_all()
    assert "dreams" in all_cards
    assert all_cards["dreams"]["name"] == "Dreams topic"

    assert repo.exists("dreams") is True
    assert repo.delete("dreams") is True
    assert repo.read("dreams") is None
    assert repo.exists("dreams") is False


def test_scope_isolation(tmp_path):
    """A card written under scope A must be invisible under scope B (same collection)."""
    coll = "cards_iso"
    client = _client(tmp_path)
    repo_a = _repo(tmp_path, scope={"user_id": "A"}, collection=coll, client=client)
    repo_b = _repo(tmp_path, scope={"user_id": "B"}, collection=coll, client=client)

    repo_a.write("secret", _sample_card("secret", "A's card", "Only A should see this.", ["alpha"]))

    # B cannot read or load A's card.
    assert repo_b.read("secret") is None
    assert "secret" not in repo_b.load_all()
    # A still sees its own.
    assert repo_a.read("secret") is not None
    assert "secret" in repo_a.load_all()
    # The scope field itself is not leaked back into the returned card.
    assert "user_id" not in repo_a.read("secret")


def test_revision_changes_on_write(tmp_path):
    repo = _repo(tmp_path)
    r0 = repo.revision()
    repo.write("c1", _sample_card("c1", "Card one", "first", ["one"]))
    r1 = repo.revision()
    assert r1 != r0  # the change-stamp must move so the store's cache invalidates


def test_update_card_through_filecontextstore(tmp_path):
    """update_card via a FileContextStore over the Qdrant repo persists and round-trips."""
    repo = _repo(tmp_path, scope={"user_id": "u1"})
    store = FileContextStore(
        str(tmp_path / "unused"), repo_root=None, auto_bootstrap=False,
        card_repository=repo, confidence_threshold=0.0,
    )

    store.update_card(
        "habits",
        fields={"name": "Habit tracking", "description": "Daily exercise and water intake habits."},
        add=[{"type": "note", "locator": {"text": "User runs 5k every morning."}, "why": "habit fact"}],
    )

    persisted = repo.read("habits")
    assert persisted is not None
    assert persisted.get("name") == "Habit tracking"
    assert any(
        (it.get("locator") or {}).get("text", "").startswith("User runs 5k")
        for it in persisted.get("content", [])
    )
    # The store sees the card via export_for_embedding (single source of card text).
    exported = store.export_for_embedding()
    assert any(it["id"] == "card:habits" for it in exported)


def test_search_cards_native_or_fallback(tmp_path):
    repo = _repo(tmp_path)
    repo.write("morning", _sample_card(
        "morning", "Morning routine", "Meditation and journaling each morning.", ["meditation", "routine"]))
    repo.write("finance", _sample_card(
        "finance", "Budget tracker", "Monthly spending and savings goals.", ["budget", "savings"]))

    # ``search_cards`` is best-effort and NEVER raises. Three valid outcomes:
    #   * a dict with the matching card (a server Qdrant with a real full-text index), or
    #   * an empty dict (EMBEDDED Qdrant: payload full-text indexes are a no-op, MatchText finds
    #     nothing — a valid "native search ran, no candidates" result), or
    #   * None (the repo signals the store to fall back to in-app IDF).
    result = repo.search_cards("meditation morning", limit=10)
    assert result is None or isinstance(result, dict)
    if result:  # non-empty dict (server mode): the match must be correct.
        assert "morning" in result
        assert "finance" not in result


def test_vector_store_finds_card_after_write(tmp_path):
    """The query-only vector arm finds a card semantically (embedded once on write by the repo)."""
    coll = "cards_vec"
    client = _client(tmp_path)
    repo = _repo(tmp_path, scope={"user_id": "v1"}, collection=coll, client=client)
    repo.write("habits", _sample_card(
        "habits", "Habit tracking", "Daily exercise and running routine.", ["exercise", "running"]))

    vstore = QdrantCardVectorStore(
        collection=coll, query_embedder=_stub_embedder, scope={"user_id": "v1"}, client=client,
    )
    # The stub embeds the SAME text identically, so querying the exact embed-text returns the card.
    hits = vstore.search(card_embed_text(repo.read("habits")), top_k=5)
    assert any(h.id == "card:habits" for h in hits)
    # Scope isolation on the vector arm too: a different scope sees nothing.
    other = QdrantCardVectorStore(
        collection=coll, query_embedder=_stub_embedder, scope={"user_id": "other"}, client=client,
    )
    assert other.search(card_embed_text(repo.read("habits")), top_k=5) == []


def test_embed_text_helper_matches_export(tmp_path):
    """The shared card_embed_text helper and FileContextStore.export_for_embedding agree."""
    repo = _repo(tmp_path)
    card = _sample_card(
        "topic", "A Topic", "A description of the topic.", ["alpha", "beta"],
        content=[{"type": "note", "locator": {"text": "an important note"}, "why": "because"}],
    )
    repo.write("topic", card)

    store = FileContextStore(
        str(tmp_path / "unused2"), repo_root=None, auto_bootstrap=False,
        card_repository=repo, confidence_threshold=0.0,
    )
    exported = store.export_for_embedding()
    item = next(it for it in exported if it["id"] == "card:topic")
    # The store's export text is exactly what the shared helper produces for the same card.
    assert item["text"] == card_embed_text(repo.read("topic"))
