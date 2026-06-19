"""Offline tests for the CardRepository persistence boundary on FileContextStore.

``FileContextStore`` owns all card LOGIC (selection / IDF / recency / the card-update API /
``export_for_embedding`` / bootstrap) but delegates raw PERSISTENCE to a ``CardRepository``. The
default ``FilesystemCardRepository`` stores one JSON file per card; a consumer can inject any other
repository (e.g. a database / Qdrant-backed one). These tests inject tiny in-memory repositories to
prove two things WITHOUT touching the filesystem:

  1. Non-filesystem persistence works end to end: a card written through the store round-trips via
     the injected repo, is selected by ``assemble``, the card-update API persists through the repo,
     and ``export_for_embedding`` reflects it. (This is what a Qdrant-backed repo will rely on.)

  2. The OPTIONAL ``search_cards`` hook: a repo that implements native text search serves the
     candidate pool for the keyword arm; a repo whose ``search_cards`` returns ``None`` (or omits it
     entirely) falls back to in-app IDF over ``load_all()``.

All assertions are additive: the filesystem behavior (test_context_assembler.py etc.) is unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quest_ai_runner.adapters.file_context_store import FileContextStore


# ---------------------------------------------------------------------------
# In-memory CardRepository implementations
# ---------------------------------------------------------------------------

class InMemoryCardRepository:
    """A dict-backed ``CardRepository`` with NO native text search.

    Every method is best-effort and never raises, mirroring the filesystem repo's error contract.
    ``revision()`` returns a counter bumped on every mutation so the store's cache invalidates.
    """

    def __init__(self) -> None:
        self._cards: Dict[str, Dict[str, Any]] = {}
        self._rev: int = 0

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        # Hand back copies so the store never mutates our stored dicts in place.
        return {cid: dict(card) for cid, card in self._cards.items()}

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        card = self._cards.get(card_id)
        return dict(card) if isinstance(card, dict) else None

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        self._cards[card_id] = dict(card)
        self._rev += 1
        return True

    def delete(self, card_id: str) -> bool:
        if card_id in self._cards:
            del self._cards[card_id]
            self._rev += 1
        return True

    def exists(self, card_id: str) -> bool:
        return card_id in self._cards

    def revision(self) -> Any:
        return self._rev


class SearchingInMemoryCardRepository(InMemoryCardRepository):
    """An in-memory repo that DOES implement native text search.

    ``search_cards`` returns only cards whose keywords overlap the query's whitespace tokens. This
    lets a test prove the store uses the repo's text-search candidates for the keyword arm (cards
    the search excludes are not pulled in). When ``disabled`` is set, ``search_cards`` returns
    ``None`` so the store falls back to in-app IDF over ``load_all()``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.disabled: bool = False
        self.calls: list = []

    def search_cards(
        self, query: str, *, limit: int
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        self.calls.append((query, limit))
        if self.disabled:
            return None
        q_tokens = {t.lower() for t in query.split() if t}
        hits: Dict[str, Dict[str, Any]] = {}
        for cid, card in self._cards.items():
            kws = {str(k).lower() for k in card.get("keywords", [])}
            if kws & q_tokens:
                hits[cid] = dict(card)
            if len(hits) >= limit:
                break
        return hits


def _card(card_id: str, keywords, summary: str = "a test card") -> Dict[str, Any]:
    return {
        "id": card_id,
        "keywords": list(keywords),
        "summary": summary,
        "files": [],
        "conventions": [],
        "provenance": {"created_by_task": "", "model": "", "created_at": "",
                       "last_verified_at": ""},
        "usage_count": 0,
        "last_outcome": "unknown",
    }


# ---------------------------------------------------------------------------
# 1. Non-filesystem persistence works end to end
# ---------------------------------------------------------------------------

class TestInjectedRepositoryPersistence:
    def test_record_persists_through_repo_and_is_selected(self, tmp_path):
        repo = InMemoryCardRepository()
        # confidence_threshold=0.0 so a small synthetic store selects on any positive match.
        store = FileContextStore(
            str(tmp_path / "unused_cards"),
            card_repository=repo,
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )

        # record() must persist via the repo (nothing on the filesystem).
        store.record("neptunium special element decay", {"kind": "met"})
        assert repo._cards, "record() did not persist through the injected repository"
        assert not (tmp_path / "unused_cards").exists(), (
            "a card file was written to disk; persistence did not go through the repo"
        )

        # The persisted card is selected by assemble().
        ac = store.assemble("neptunium special element")
        assert ac.card_ids, "card persisted through repo was not selected by assemble()"

    def test_update_card_and_add_content_round_trip_through_repo(self, tmp_path):
        repo = InMemoryCardRepository()
        store = FileContextStore(
            str(tmp_path / "cards"),
            card_repository=repo,
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )

        # add_content creates the card through the repo when absent.
        assert store.add_content("topic-card", {
            "type": "note", "id": "n1", "why": "first note",
            "locator": {"text": "alpha beta gamma"}, "ts": 1.0,
        })
        stored = repo.read("topic-card")
        assert stored is not None
        assert any(it.get("id") == "n1" for it in stored.get("content", []))

        # update_card edits embedded fields + appends content in one read-modify-write.
        assert store.update_card(
            "topic-card",
            fields={"summary": "updated summary keywords", "name": "Topic Card"},
            add=[{"type": "note", "id": "n2", "why": "second",
                  "locator": {"text": "delta"}, "ts": 2.0}],
        )
        stored = repo.read("topic-card")
        assert stored.get("summary") == "updated summary keywords"
        assert {it.get("id") for it in stored.get("content", [])} >= {"n1", "n2"}

        # export_for_embedding reflects the card persisted in the repo.
        items = store.export_for_embedding()
        ids = {it["id"] for it in items}
        assert "card:topic-card" in ids, "export_for_embedding did not reflect the repo-stored card"
        exported = next(it for it in items if it["id"] == "card:topic-card")
        # Embedded text includes the name + summary; content text folds in too.
        assert "Topic Card" in exported["text"]
        assert "updated summary keywords" in exported["text"]

    def test_external_repo_write_invalidates_cache(self, tmp_path):
        repo = InMemoryCardRepository()
        store = FileContextStore(
            str(tmp_path / "cards"),
            card_repository=repo,
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )
        # Warm the cache with one assemble().
        store.record("alpha beta gamma topic", {"kind": "met"})
        _ = store.assemble("alpha beta gamma")

        # Simulate ANOTHER process writing directly to the repo (bumps revision()).
        repo.write("external-card", _card("external-card", ["zeta", "omega", "external"]))

        # The store must notice the revision change and reload, seeing the external card.
        ac = store.assemble("zeta omega external")
        assert "external-card" in ac.card_ids, (
            "external repo write not detected; the revision()-based cache invalidation regressed"
        )


# ---------------------------------------------------------------------------
# 2. OPTIONAL native text-search hook
# ---------------------------------------------------------------------------

class TestRepoTextSearchHook:
    def test_keyword_arm_uses_repo_search_candidates(self, tmp_path):
        repo = SearchingInMemoryCardRepository()
        # Two cards. The repo's native search returns only the card whose keywords overlap the
        # query token ("neptunium"); "excluded" has a disjoint keyword set so search drops it,
        # proving the keyword arm draws its candidates from the repo's native search.
        repo.write("wanted", _card("wanted", ["neptunium", "element", "decay"]))
        repo.write("excluded", _card("excluded", ["sodium", "metal", "salt"]))

        store = FileContextStore(
            str(tmp_path / "cards"),
            card_repository=repo,
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )

        ac = store.assemble("neptunium")
        assert repo.calls, "store did not call the repo's native search_cards hook"
        assert "wanted" in ac.card_ids, "repo text-search candidate was not selected"
        assert "excluded" not in ac.card_ids, (
            "a card the repo's search excluded was pulled into the keyword arm anyway"
        )

    def test_search_cards_returning_none_falls_back_to_idf(self, tmp_path):
        repo = SearchingInMemoryCardRepository()
        repo.disabled = True  # search_cards now returns None
        repo.write("alpha", _card("alpha", ["alpha", "topic", "data"]))
        repo.write("beta", _card("beta", ["beta", "other", "thing"]))

        store = FileContextStore(
            str(tmp_path / "cards"),
            card_repository=repo,
            auto_bootstrap=False,
            confidence_threshold=0.0,
        )

        ac = store.assemble("alpha topic data")
        assert repo.calls, "search_cards should still be consulted (and return None)"
        # Fallback to in-app IDF over load_all(): the matching card is selected and ranks first
        # (the non-matching card may still appear at threshold 0.0 with a zero score, but the
        # matching card must out-rank it — proving IDF ranked the full load_all() pool).
        assert ac.card_ids, "fallback IDF selected nothing"
        assert ac.card_ids[0] == "alpha", "in-app IDF fallback did not rank the matching card first"

    def test_filesystem_repo_has_no_search_cards(self):
        from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
        fs = FilesystemCardRepository("/tmp/does-not-matter")
        assert not hasattr(fs, "search_cards"), (
            "FilesystemCardRepository must NOT implement search_cards so it always uses in-app IDF"
        )
