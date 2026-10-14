"""Offline tests for ``QuestApiCardRepository`` against an in-process fake of the card API.

What they prove: the CardRepository protocol (a ``{card_id: card}`` mapping, the store's call
signatures), the access-scope plumbing (``team_id`` on write, private when the lane has no team),
paging plus delta refresh driven by ``/api/cards/stamp``, bulk batching, and that a backend failure
is LOUD (ERROR log, ``last_error``, ``error_count``, the ``on_error`` callback, and a source entry in
the run's context) instead of silently reading as "no cards".
"""
import logging

import pytest

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.quest_api_card_repository import (
    BULK_MAX_CARDS, NullCardCache, QuestApiCardRepository, QuestApiCardVectorStore)
from tests.quest_card_backend_fake import FakeCardBackend

CARD_A = {"id": "morning-routine", "name": "Morning Routine", "summary": "Before 9am."}
CARD_B = {"id": "production-bugs", "name": "Production Bugs", "summary": "Server crashes."}


@pytest.fixture
def backend():
    fake = FakeCardBackend()
    yield fake
    fake.close()


def make_repo(backend, **kw):
    return QuestApiCardRepository(base_url=backend.url, api_key="k", **kw)


def paths(backend, method=None):
    return [(m, p) for m, p, *_ in backend.requests if method in (None, m)]


# --- protocol -------------------------------------------------------------------

def test_load_all_returns_the_mapping_the_protocol_requires(backend):
    backend.put("morning-routine", CARD_A)
    backend.put("production-bugs", CARD_B)
    cards = make_repo(backend).load_all()
    assert isinstance(cards, dict)
    assert set(cards) == {"morning-routine", "production-bugs"}
    assert [c["name"] for c in cards.values()] == ["Morning Routine", "Production Bugs"]


def test_older_mapping_reply_shape_is_still_accepted():
    from quest_ai_runner.adapters.quest_api_card_repository import _cards_by_id
    assert _cards_by_id({"k": {"name": "No id"}}) == {"k": {"name": "No id", "id": "k"}}
    assert _cards_by_id({"cards": "junk"}) == {}
    assert _cards_by_id(["not a card"]) == {}


def test_search_cards_is_callable_the_way_the_store_calls_it_and_asks_for_managed(backend):
    backend.put("morning-routine", CARD_A)
    repo = make_repo(backend)
    result = repo.search_cards("morning", limit=32)
    assert list(result) == ["morning-routine"]
    q = [r for r in backend.requests if r[1] == "/api/cards/search"][-1][2]
    assert q["max_results"] == "32" and q["include_managed"] == "true"


def test_the_context_store_reads_through_this_repository(tmp_path, backend):
    backend.put("morning-routine", CARD_A)
    backend.put("production-bugs", CARD_B)
    store = FileContextStore(str(tmp_path / "cards"), auto_bootstrap=False,
                             card_repository=make_repo(backend))
    assert set(store._load_all()) == {"morning-routine", "production-bugs"}


# --- identity and scope ------------------------------------------------------------

def test_user_is_resolved_from_the_key_and_sent_as_the_acting_user(backend):
    repo = make_repo(backend)
    repo.load_all()
    assert repo.user_id == "user_a"
    later = [r for r in backend.requests if r[1] == "/api/cards/stamp"][0]
    assert later[3].get("X-User-Id") == "user_a"


def test_a_user_hint_that_differs_from_the_key_loses_with_a_warning(backend, caplog):
    repo = make_repo(backend, user_id="someone_else")
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.quest_api_cards"):
        assert repo.resolve_user_id() == "user_a"
    assert "different user" in caplog.text


def test_a_lane_with_a_team_writes_team_cards(backend):
    repo = make_repo(backend, team_id="team_a")
    assert repo.write("c1", {"name": "One", "summary": "s"}) is True
    method, path, query, _h, body = [r for r in backend.requests if r[0] == "PUT"][0]
    assert path == "/api/cards/c1" and query == {"team_id": "team_a"}
    assert body["team_id"] == "team_a" and body["runner_env_id"]
    assert ("team_a", "c1") in backend.cards


def test_a_lane_without_a_team_writes_private_cards(backend):
    repo = make_repo(backend)
    assert repo.write("c1", {"name": "One"}) is True
    method, path, query, _h, body = [r for r in backend.requests if r[0] == "PUT"][0]
    assert query == {} and "team_id" not in body
    assert ("", "c1") in backend.cards


def test_server_owned_fields_are_not_echoed_back(backend):
    repo = make_repo(backend)
    repo.write("c1", {"name": "One", "updated_at": "x", "revision": "y", "owner_user_id": "z",
                      "user_id": "q", "card_id": "c1"})
    body = [r for r in backend.requests if r[0] == "PUT"][0][4]
    for key in ("updated_at", "revision", "owner_user_id", "user_id", "card_id"):
        assert key not in body


def test_updating_a_private_card_listed_earlier_stays_private_in_a_team_lane(backend):
    backend.put("mine", {"name": "Mine"})
    repo = make_repo(backend, team_id="team_a")
    cards = repo.load_all()
    repo.write("mine", cards["mine"])
    assert ("", "mine") in backend.cards and ("team_a", "mine") not in backend.cards


def test_runner_env_id_is_read_from_qar_env_id(monkeypatch, backend):
    monkeypatch.delenv("QAR_RUNNER_ENV_ID", raising=False)
    monkeypatch.setenv("QAR_ENV_ID", "env_x")
    make_repo(backend).write("c1", {"name": "One"})
    body = [r for r in backend.requests if r[0] == "PUT"][0][4]
    assert body["runner_env_id"] == "env_x"
    monkeypatch.setenv("QAR_RUNNER_ENV_ID", "env_y")
    make_repo(backend).write("c2", {"name": "Two"})
    assert [r for r in backend.requests if r[0] == "PUT"][1][4]["runner_env_id"] == "env_y"


# --- paging, stamp, delta -------------------------------------------------------------

def test_first_load_pages_through_every_card(backend):
    backend.page_size_cap = 2
    for i in range(5):
        backend.put(f"c{i}", {"name": f"Card {i}"})
    cards = make_repo(backend).load_all()
    assert len(cards) == 5
    lists = [r for r in backend.requests if r[1] == "/api/cards"]
    assert len(lists) == 3 and all(r[2]["limit"] for r in lists)


def test_a_later_refresh_fetches_only_what_changed_then_a_deletion_forces_one_full_refresh(backend, monkeypatch):
    for i in range(3):
        backend.put(f"c{i}", {"name": f"Card {i}"})
    repo = make_repo(backend)
    assert len(repo.load_all()) == 3
    backend.requests.clear()

    backend.put("c3", {"name": "Card 3"})
    repo._listing_at = 0.0
    repo._stamp_at = 0.0
    assert len(repo.load_all()) == 4
    lists = [r for r in backend.requests if r[1] == "/api/cards"]
    assert len(lists) == 1 and float(lists[0][2]["updated_after"]) > 0

    backend.requests.clear()
    del backend.cards[("", "c0")]
    repo._listing_at = 0.0
    repo._stamp_at = 0.0
    cards = repo.load_all()
    assert "c0" not in cards and len(cards) == 3
    lists = [r for r in backend.requests if r[1] == "/api/cards"]
    assert "updated_after" not in lists[-1][2]      # the count mismatch triggered a full refresh


def test_revision_uses_the_stamp_endpoint_and_changes_with_writes(backend):
    backend.put("c0", {"name": "Zero"})
    repo = make_repo(backend)
    first = repo.revision()
    assert [r for r in backend.requests if r[1] == "/api/cards/stamp"]
    assert not [r for r in backend.requests if r[1] == "/api/cards"]
    repo.write("c1", {"name": "One"})
    assert repo.revision() != first


# --- loud failures ---------------------------------------------------------------------

def test_a_failed_listing_is_logged_counted_surfaced_and_serves_the_last_good_listing(backend, caplog):
    backend.put("c0", {"name": "Zero"})
    seen = []
    repo = make_repo(backend, on_error=seen.append)
    assert set(repo.load_all()) == {"c0"}
    repo._listing_at = 0.0
    repo._stamp_at = 0.0
    backend.fail_next = [(503, "qdrant unavailable")]
    with caplog.at_level(logging.ERROR, logger="quest-ai-runner.quest_api_cards"):
        cards = repo.load_all()
    assert set(cards) == {"c0"}                                  # last good, not silently empty
    assert repo.error_count == 1 and repo.last_error["status"] == 503
    assert "qdrant unavailable" in repo.last_error["detail"]
    assert repo.health()["degraded"] is True
    assert any(r.levelno == logging.ERROR and "qdrant unavailable" in r.getMessage() for r in caplog.records)
    assert seen and "503" in seen[0]
    repo._listing_at = 0.0
    repo._stamp_at = 0.0
    repo.load_all()
    assert repo.health()["degraded"] is False                    # clears on the next success


def test_an_unreachable_backend_is_loud_on_every_operation():
    repo = QuestApiCardRepository(base_url="http://127.0.0.1:9", api_key="k", timeout=0.5)
    assert repo.write("c", {"name": "x"}) is False
    assert repo.read("c") is None
    assert repo.delete("c") is False
    assert repo.search_cards("q", limit=3) is None
    assert repo.load_all() == {}
    assert repo.error_count >= 5 and repo.last_error["status"] == 0


def test_a_missing_card_is_not_counted_as_an_outage(backend):
    repo = make_repo(backend)
    assert repo.read("absent") is None
    assert repo.error_count == 0


def test_a_failing_backend_shows_up_in_the_runs_context_sources(tmp_path, backend):
    backend.put("morning-routine", CARD_A)
    repo = make_repo(backend)
    store = FileContextStore(str(tmp_path / "cards"), auto_bootstrap=False, card_repository=repo)
    store.assemble("morning routine")
    backend.fail_next = [(500, "boom")] * 6
    repo._listing_at = repo._stamp_at = 0.0
    result = store.assemble("morning routine")
    labels = [s["label"] for s in result.sources if s["adapter"] == "card_backend"]
    assert labels and "CARD BACKEND ERROR" in labels[0] and "boom" in labels[0]


def test_the_cache_seam_defaults_to_a_null_implementation(backend):
    cache = NullCardCache()
    assert cache.load_listing() is None and cache.enqueue_write("a", {}, "") is False
    assert cache.pending_writes() == []


# --- bulk ----------------------------------------------------------------------------------

def test_bulk_write_batches_at_100_and_offers_vectors_with_the_embedder(backend):
    repo = make_repo(backend, team_id="team_a")
    pairs = [(f"c{i}", {"name": f"Card {i}"}) for i in range(250)]
    vectors = {f"c{i}": [0.1, 0.2, 0.3] for i in range(250)}
    result = repo.bulk_write(pairs, vectors, embedder_id="voyage:test-model", dimension=3)
    assert result["batches"] == 3 and [len(b["cards"]) for b in backend.bulk_batches] == [100, 100, 50]
    assert all(b["team_id"] == "team_a" and b["embedder_id"] == "voyage:test-model"
               for b in backend.bulk_batches)
    assert result["counts"]["written"] == 250 and result["counts"]["vectors_accepted"] == 250
    assert not result["failed"] and BULK_MAX_CARDS == 100


def test_bulk_write_without_vectors_sends_none_and_a_failed_batch_is_reported(backend):
    repo = make_repo(backend)
    repo.resolve_user_id()
    backend.fail_next = [(500, "bulk down")]
    result = repo.bulk_write([("a", {"name": "A"}), ("b", {"name": "B"})])
    assert result["failed"] == ["a", "b"] and result["counts"]["failed"] == 2
    assert "team_id" not in backend.requests[-1][4] and "embedder_id" not in backend.requests[-1][4]
    assert repo.error_count == 1


# --- server-backed vector arm -----------------------------------------------------------------

def test_the_vector_arm_hands_the_servers_ranking_through_without_embedding(backend):
    backend.put("morning-routine", CARD_A)
    backend.put("production-bugs", CARD_B)
    hits = QuestApiCardVectorStore(make_repo(backend)).search("bugs", top_k=5)
    assert [h.payload["card_id"] for h in hits] == ["production-bugs"]
    assert hits[0].score <= 1.0
