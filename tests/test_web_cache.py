"""Tests for WebCache -- offline, no network."""
from __future__ import annotations

import json
import time

from quest_ai_runner.adapters.web_cache import WebCache, normalize_query


# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------


def test_normalize_query_lowercases_and_collapses_whitespace():
    assert normalize_query("  What    is   the Weather?  ") == "what is the weather"


def test_normalize_query_strips_surrounding_quotes_and_punctuation():
    assert normalize_query('"best keyboards 2024."') == "best keyboards 2024"


def test_normalize_query_empty():
    assert normalize_query("") == ""
    assert normalize_query(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Memory-only cache: hit / miss / expiry / fresh
# ---------------------------------------------------------------------------


def test_memory_miss_then_hit():
    cache = WebCache()
    assert cache.get("ns", "k1") is None
    cache.set("ns", "k1", {"v": 1}, ttl_seconds=60)
    assert cache.get("ns", "k1") == {"v": 1}


def test_memory_expiry():
    cache = WebCache()
    cache.set("ns", "k1", "value", ttl_seconds=0.01)
    time.sleep(0.05)
    assert cache.get("ns", "k1") is None


def test_namespaces_are_independent():
    cache = WebCache()
    cache.set("search", "same-key", "search-value", ttl_seconds=60)
    cache.set("page", "same-key", "page-value", ttl_seconds=60)
    assert cache.get("search", "same-key") == "search-value"
    assert cache.get("page", "same-key") == "page-value"


def test_fresh_semantics_handled_by_caller_set_still_writes():
    # WebCache itself has no "fresh" concept; callers skip the .get() call to bypass the
    # read while still calling .set() to refresh the entry. Confirm set() always overwrites.
    cache = WebCache()
    cache.set("ns", "k1", "old", ttl_seconds=60)
    cache.set("ns", "k1", "new", ttl_seconds=60)
    assert cache.get("ns", "k1") == "new"


def test_lru_eviction_over_max_entries():
    cache = WebCache(max_entries=2)
    cache.set("ns", "a", 1, ttl_seconds=60)
    cache.set("ns", "b", 2, ttl_seconds=60)
    cache.set("ns", "c", 3, ttl_seconds=60)  # evicts "a"
    assert cache.get("ns", "a") is None
    assert cache.get("ns", "b") == 2
    assert cache.get("ns", "c") == 3


def test_stats_reports_hits_and_misses():
    cache = WebCache()
    cache.set("ns", "k1", "v", ttl_seconds=60)
    cache.get("ns", "k1")  # hit
    cache.get("ns", "missing")  # miss
    stats = cache.stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1
    assert stats["mem_entries"] == 1
    assert stats["directory"] is None


# ---------------------------------------------------------------------------
# Disk tier: round trip, corrupt file, prune, directory property
# ---------------------------------------------------------------------------


def test_disk_round_trip_across_instances(tmp_path):
    cache1 = WebCache(directory=tmp_path)
    cache1.set("ns", "k1", {"a": [1, 2, 3]}, ttl_seconds=3600)

    # A fresh instance (simulating a process restart) with an empty memory tier should
    # still find the value on disk.
    cache2 = WebCache(directory=tmp_path)
    assert cache2.get("ns", "k1") == {"a": [1, 2, 3]}


def test_disk_directory_property(tmp_path):
    cache = WebCache(directory=tmp_path)
    assert cache.directory is not None
    assert cache.directory.exists()

    mem_only = WebCache()
    assert mem_only.directory is None


def test_disk_expired_entry_is_a_miss_and_cleaned_up(tmp_path):
    cache = WebCache(directory=tmp_path)
    cache.set("ns", "k1", "value", ttl_seconds=0.01)
    time.sleep(0.05)

    # Force a fresh instance so the (expired) memory entry doesn't short-circuit the disk read.
    cache2 = WebCache(directory=tmp_path)
    assert cache2.get("ns", "k1") is None
    assert list(tmp_path.glob("*.json")) == []


def test_disk_corrupt_file_is_a_miss(tmp_path):
    cache = WebCache(directory=tmp_path)
    cache.set("ns", "k1", "value", ttl_seconds=3600)

    # Corrupt the single cache file on disk.
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    files[0].write_text("not valid json {{{", encoding="utf-8")

    cache2 = WebCache(directory=tmp_path)
    assert cache2.get("ns", "k1") is None


def test_disk_prune_keeps_disk_entries_bounded(tmp_path):
    # Pruning is amortized (every ~50 writes), not enforced on every write, so the count isn't
    # strictly <= max_disk_entries at all times -- but it must not grow unboundedly: after
    # several prune cycles it settles back down near the cap instead of tracking every write.
    cache = WebCache(directory=tmp_path, max_entries=1000, max_disk_entries=5)
    for i in range(200):
        cache.set("ns", f"k{i}", i, ttl_seconds=3600)
    remaining = list(tmp_path.glob("*.json"))
    assert len(remaining) < 200  # pruning happened at all
    assert len(remaining) <= 5 + 50  # bounded by the cap plus one amortization window


def test_disk_write_is_atomic_no_tmp_files_left_behind(tmp_path):
    cache = WebCache(directory=tmp_path)
    cache.set("ns", "k1", "value", ttl_seconds=3600)
    tmp_files = list(tmp_path.glob(".webcache-*"))
    assert tmp_files == []


def test_disk_file_is_valid_json(tmp_path):
    cache = WebCache(directory=tmp_path)
    cache.set("ns", "k1", {"nested": True}, ttl_seconds=3600)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    raw = json.loads(files[0].read_text(encoding="utf-8"))
    assert raw["value"] == {"nested": True}
    assert "expires_at" in raw


def test_disk_prune_never_deletes_a_file_this_cache_did_not_write(tmp_path):
    """The daily-limit counter lives in this same directory as web_search_daily_count.json;
    pruning it would silently reset the day's cost guard to zero."""
    counter = tmp_path / "web_search_daily_count.json"
    counter.write_text(json.dumps({"day": "2026-10-06", "count": 1400}), encoding="utf-8")
    cache = WebCache(directory=tmp_path, max_entries=1000, max_disk_entries=2)
    for i in range(120):
        cache.set("ns", f"k{i}", i, ttl_seconds=3600)
    assert counter.exists()
    assert json.loads(counter.read_text(encoding="utf-8"))["count"] == 1400
    assert cache.stats()["disk_entries"] == len(
        [p for p in tmp_path.glob("*.json") if p.name != counter.name]
    )
