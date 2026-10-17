"""FileCardCache: the durable CardCache, and the repository serving its stored listing on a cold start."""
import pytest

from quest_ai_runner.adapters.card_cache_file import FileCardCache
from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
from tests.quest_card_backend_fake import FakeCardBackend


@pytest.fixture
def backend():
    fake = FakeCardBackend()
    yield fake
    fake.close()


def test_listing_round_trips_across_instances(tmp_path):
    FileCardCache(str(tmp_path)).store_listing({"a": {"name": "A"}}, 12.5)
    assert FileCardCache(str(tmp_path)).load_listing() == ({"a": {"name": "A"}}, 12.5)


def test_empty_or_corrupt_directory_gives_no_listing(tmp_path):
    assert FileCardCache(str(tmp_path / "missing")).load_listing() is None
    (tmp_path / "listing.json").write_text("{not json", encoding="utf-8")
    assert FileCardCache(str(tmp_path)).load_listing() is None


def test_outbox_keeps_order_and_replaces_a_rewrite_of_the_same_card(tmp_path):
    cache = FileCardCache(str(tmp_path))
    assert cache.enqueue_write("x", {"v": 1}, "t") and cache.enqueue_write("y", {"v": 1}, "t")
    cache.enqueue_write("x", {"v": 2}, "t")
    assert FileCardCache(str(tmp_path)).pending_writes() == [("x", {"v": 2}, "t"), ("y", {"v": 1}, "t")]


def test_an_unwritable_directory_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    cache = FileCardCache(str(blocker / "sub"))
    cache.store_listing({"a": {}}, 1.0)
    assert cache.enqueue_write("a", {}, "") is False
    assert cache.load_listing() is None


def test_cold_start_during_an_outage_serves_the_stored_listing_then_recovers(tmp_path):
    backend = FakeCardBackend()
    backend.put("a", {"name": "a"})
    first = QuestApiCardRepository(base_url=backend.url, api_key="k", cache=FileCardCache(str(tmp_path)),
                                   timeout=2.0)
    assert sorted(first.load_all()) == ["a"]
    url = backend.url
    backend.close()
    second = QuestApiCardRepository(base_url=url, api_key="k", cache=FileCardCache(str(tmp_path)),
                                    timeout=2.0)
    assert sorted(second.load_all()) == ["a"]
    assert second.health()["degraded"] is True
