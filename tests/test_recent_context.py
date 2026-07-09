"""Offline tests for core/recent_context.py: FileRecentContextStore, filter_relevant,
render_recent_cards. No network, no real paths/ids -- everything lives under tmp_path."""
from __future__ import annotations

import datetime
import hashlib
import json

from quest_ai_runner.core.recent_context import (
    FileRecentContextStore,
    filter_relevant,
    render_recent_cards,
)


def _seed_raw_file(tmp_path, key: str, turns: list) -> None:
    """Write a turns file directly (bypassing record()) so tests can control ``ts``/shape."""
    recent_dir = tmp_path / "recent"
    recent_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    (recent_dir / f"{digest}.json").write_text(
        json.dumps({"turns": turns}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# FileRecentContextStore
# ---------------------------------------------------------------------------


def test_record_load_round_trip(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    cards = [{
        "id": "card-1",
        "title": "Refund policy",
        "adapter": "keyword",
        "relevance_score": 0.87,
        "files": ["docs/refunds.md"],
        "rendered_section": "Refunds are processed within 5 business days.",
    }]
    store.record("conv-1", cards, "what is the refund policy?")

    loaded = store.load("conv-1")
    assert len(loaded) == 1
    rec = loaded[0]
    assert rec["id"] == "card-1"
    assert rec["title"] == "Refund policy"
    assert rec["adapter"] == "keyword"
    assert rec["relevance_score"] == 0.87
    assert rec["files"] == ["docs/refunds.md"]
    assert rec["preview"] == "Refunds are processed within 5 business days."
    assert rec["turn_user_text"] == "what is the refund policy?"
    assert "ts" in rec
    assert rec["turn_index"] == 0
    assert "refund" in rec["keywords"]


def test_turn_cap_prunes_oldest_turns(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path), max_turns=3)
    for i in range(5):
        store.record("conv-1", [{"id": f"card-{i}", "title": f"topic {i}"}], f"question {i}")

    loaded = store.load("conv-1")
    ids = {r["id"] for r in loaded}
    # Only the last 3 recorded turns survive max_turns=3.
    assert ids == {"card-2", "card-3", "card-4"}


def test_max_cards_caps_and_trims_older_turns_first(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path), max_cards=2)
    store.record("conv-1", [{"id": "a", "title": "Alpha"}], "q1")
    store.record("conv-1", [{"id": "b", "title": "Beta"}], "q2")
    store.record("conv-1", [{"id": "c", "title": "Gamma"}], "q3")

    loaded = store.load("conv-1")
    ids = {r["id"] for r in loaded}
    # max_cards=2: walking newest-turn-first, "c" and "b" fill the cap; "a" (oldest) is trimmed.
    assert ids == {"b", "c"}


def test_duplicate_id_newest_content_wins(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-1", [{"id": "x", "title": "Old title", "rendered_section": "old preview"}], "q1")
    store.record("conv-1", [{"id": "x", "title": "New title", "rendered_section": "new preview"}], "q2")

    loaded = store.load("conv-1")
    assert len(loaded) == 1
    assert loaded[0]["title"] == "New title"
    assert loaded[0]["preview"] == "new preview"
    assert loaded[0]["turn_index"] == 0


def test_ttl_prune_drops_old_turns(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path), max_record_age_days=14.0)
    old_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    ).isoformat().replace("+00:00", "Z")
    _seed_raw_file(tmp_path, "conv-1", [
        {"ts": old_ts, "user_text": "ancient question", "cards": [{"id": "old-card", "title": "Old"}]},
    ])

    # Any record() call re-loads existing turns and prunes ones older than max_record_age_days.
    store.record("conv-1", [{"id": "new-card", "title": "New"}], "fresh question")

    loaded = store.load("conv-1")
    ids = {r["id"] for r in loaded}
    assert ids == {"new-card"}


def test_corrupt_file_returns_empty(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    recent_dir = tmp_path / "recent"
    recent_dir.mkdir(parents=True)
    digest = hashlib.sha1("conv-1".encode("utf-8")).hexdigest()[:16]
    (recent_dir / f"{digest}.json").write_text("{not valid json!!", encoding="utf-8")

    assert store.load("conv-1") == []


def test_record_noop_on_empty_key(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("", [{"id": "a", "title": "A"}], "q")
    assert not (tmp_path / "recent").exists()


def test_record_noop_on_empty_cards(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-1", [], "q")
    assert not (tmp_path / "recent").exists()


def test_load_empty_key_returns_empty(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    assert store.load("") == []


def test_preview_capped_at_1500_chars(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    long_text = "x" * 2000
    store.record("conv-1", [{"id": "a", "title": "Long", "rendered_section": long_text}], "q")

    loaded = store.load("conv-1")
    preview = loaded[0]["preview"]
    assert len(preview) <= 1501  # 1500 chars + the ellipsis marker
    assert preview.endswith("…")


def test_turn_index_stamping_most_recent_is_zero(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-1", [{"id": "a", "title": "First"}], "q1")
    store.record("conv-1", [{"id": "b", "title": "Second"}], "q2")
    store.record("conv-1", [{"id": "c", "title": "Third"}], "q3")

    loaded = store.load("conv-1")
    by_id = {r["id"]: r["turn_index"] for r in loaded}
    assert by_id == {"c": 0, "b": 1, "a": 2}


# ---------------------------------------------------------------------------
# filter_relevant
# ---------------------------------------------------------------------------


def test_followup_forces_only_last_turn_cards():
    records = [
        {"id": "a", "turn_index": 0, "title": "unrelated topic zero", "keywords": ["zzzcompletely", "unrelated"]},
        {"id": "b", "turn_index": 1, "title": "unrelated topic one", "keywords": ["zzzcompletely", "unrelated"]},
    ]
    result = filter_relevant(records, "totally different subject entirely", is_followup=True, max_cards=6)
    ids = {r["id"] for r in result}
    assert ids == {"a"}  # turn_index 0 forced through; the older turn has no overlap so it is dropped


def test_followup_older_turn_still_needs_and_gets_overlap():
    records = [
        {"id": "a", "turn_index": 0, "title": "billing questions", "keywords": ["billing", "invoice"]},
        {"id": "b", "turn_index": 1, "title": "refund policy details", "keywords": ["refund", "policy"]},
    ]
    result = filter_relevant(records, "refund policy", is_followup=True, max_cards=6)
    ids = {r["id"] for r in result}
    assert ids == {"a", "b"}  # a: forced (turn_index 0); b: real lexical overlap with the query


def test_lexical_pass_by_ratio_with_single_overlapping_token():
    # Query has 6 informative tokens; only "python" overlaps -> ratio 1/6 = 0.167 >= 0.15 threshold,
    # even though the overlap COUNT is only 1 (below the count-based threshold of 2).
    record = {"id": "a", "turn_index": 5, "title": "", "keywords": ["python"]}
    result = filter_relevant(
        [record], "python testing frameworks django flask pytest", is_followup=False, max_cards=6)
    assert [r["id"] for r in result] == ["a"]


def test_lexical_pass_by_two_token_count_below_ratio_threshold():
    # Query has 16 informative tokens; exactly 2 ("python", "testing") overlap -> ratio 2/16 = 0.125
    # is BELOW the 0.15 ratio threshold, but the >=2 distinct-token count threshold still passes it.
    query = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron "
             "python testing")
    record = {"id": "a", "turn_index": 5, "title": "", "keywords": ["python", "testing"]}
    result = filter_relevant([record], query, is_followup=False, max_cards=6)
    assert [r["id"] for r in result] == ["a"]


def test_unrelated_query_returns_none():
    records = [{"id": "a", "turn_index": 3, "title": "pricing tiers", "keywords": ["pricing", "tiers"]}]
    result = filter_relevant(records, "completely different unrelated subject today", is_followup=False,
                             max_cards=6)
    assert result == []


def test_max_cards_cap_is_enforced():
    records = [
        {"id": f"card-{i}", "turn_index": 0, "title": "shared topic", "keywords": ["shared", "topic"]}
        for i in range(10)
    ]
    result = filter_relevant(records, "shared topic query", is_followup=False, max_cards=3)
    assert len(result) == 3


def test_filter_relevant_never_raises_on_malformed_records():
    malformed = [None, 42, "just a string", {}, {"id": "ok", "turn_index": 0}]
    result = filter_relevant(malformed, "some query text", is_followup=True, max_cards=6)
    assert isinstance(result, list)
    assert [r["id"] for r in result] == ["ok"]  # only the well-formed, forced record survives


def test_filter_relevant_empty_records_returns_empty():
    assert filter_relevant([], "anything", is_followup=True, max_cards=6) == []


# ---------------------------------------------------------------------------
# render_recent_cards
# ---------------------------------------------------------------------------


def test_render_recent_cards_block_and_entries_shape():
    records = [{
        "id": "card-1",
        "title": "Refund policy",
        "preview": "Refunds within 5 days.",
        "relevance_score": 0.9,
        "files": ["docs/a.md"],
    }]
    text, entries = render_recent_cards(records)

    assert "CONTEXT FROM RECENT TURNS" in text
    assert "Refund policy" in text
    assert "Refunds within 5 days." in text

    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "card-1"
    assert entry["title"] == "Refund policy"
    assert entry["adapter"] == "recent"
    assert entry["file_count"] == 1
    assert entry["files"] == ["docs/a.md"]
    assert "rendered_section" in entry


def test_render_recent_cards_empty_returns_empty():
    text, entries = render_recent_cards([])
    assert text == ""
    assert entries == []


def test_render_recent_cards_title_falls_back_to_id():
    records = [{"id": "card-x", "preview": "some preview"}]
    text, entries = render_recent_cards(records)
    assert "card-x" in text
    assert entries[0]["title"] == "card-x"
