"""Offline tests for core/recent_context.py: FileRecentContextStore, filter_relevant,
render_recent_cards, build_item_usage_hint. No network, no real paths/ids -- everything lives
under tmp_path.

A bare string key (e.g. "conv-1") is still accepted everywhere a scope key is documented, for
convenience/back-compat with simple single-scope callers; it is classified as the "conv" scope
(same caps/weight as before this module grew scopes). The newer ``"conv:<id>"``/``"quest:<id>"``/
``"global"`` scope-key format is what the Orchestrator actually builds (see
core/orchestrator.py's ``_recent_scope_keys``) and is what the scope-specific tests below use.
"""
from __future__ import annotations

import datetime
import hashlib
import json

from quest_ai_runner.core.recent_context import (
    FileRecentContextStore,
    build_item_usage_hint,
    conv_scope_key,
    filter_relevant,
    quest_scope_key,
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
# FileRecentContextStore -- basic record/load round trip (bare-key convenience)
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
    assert rec["scope"] == "conv"  # a bare key defaults to the conv scope
    assert "refund" in rec["keywords"]
    assert rec["items"] == []  # no structured items on this card -> falls back to whole-card preview


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


def test_load_empty_list_returns_empty(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    assert store.load([]) == []


def test_whole_card_preview_capped_at_500_chars(tmp_path):
    # A card with NO structured items falls back to a whole-card preview, capped at 500 chars
    # (down from the old flat 1500-char cap now that items carry most of the per-turn memory).
    store = FileRecentContextStore(root_dir=str(tmp_path))
    long_text = "x" * 2000
    store.record("conv-1", [{"id": "a", "title": "Long", "rendered_section": long_text}], "q")

    loaded = store.load("conv-1")
    preview = loaded[0]["preview"]
    assert len(preview) <= 501  # 500 chars + the ellipsis marker
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
# Item-level usage memory: capture, capping, and union across turns.
# ---------------------------------------------------------------------------


def _card_with_items(card_id: str, items: list) -> dict:
    return {"id": card_id, "title": "Card " + card_id, "adapter": "keyword", "items": items}


def test_item_capture_shape_and_preview_cap(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    long_text = "y" * 500
    items = [
        {"id": "i1", "type": "file", "locator": {"path": "a.py"}, "text": "short body"},
        {"id": "i2", "type": "note", "locator": {"text": "raw note text"}, "text": long_text},
    ]
    store.record("conv-1", [_card_with_items("card-1", items)], "how does the refund flow work")

    loaded = store.load("conv-1")
    assert len(loaded) == 1
    rec_items = {it["id"]: it for it in loaded[0]["items"]}
    assert set(rec_items) == {"i1", "i2"}
    assert rec_items["i1"]["type"] == "file"
    assert rec_items["i1"]["locator"] == {"path": "a.py"}
    assert rec_items["i1"]["preview"] == "short body"
    assert "last_used_ts" in rec_items["i1"]
    assert "refund" in rec_items["i1"]["input_keywords"]
    # Item preview capped at 300 chars.
    assert len(rec_items["i2"]["preview"]) <= 301
    assert rec_items["i2"]["preview"].endswith("…")
    # A card WITH items does not fall back to a whole-card preview.
    assert loaded[0]["preview"] == ""


def test_item_count_capped_at_eight_per_card(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    items = [{"id": f"i{i}", "type": "note", "locator": {}, "text": f"item {i}"} for i in range(12)]
    store.record("conv-1", [_card_with_items("card-1", items)], "q")

    loaded = store.load("conv-1")
    assert len(loaded[0]["items"]) == 8


def test_note_locator_text_truncated(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    long_note = "z" * 500
    items = [{"id": "i1", "type": "note", "locator": {"text": long_note}, "text": "preview text"}]
    store.record("conv-1", [_card_with_items("card-1", items)], "q")

    loaded = store.load("conv-1")
    stored_text = loaded[0]["items"][0]["locator"]["text"]
    assert len(stored_text) <= 201
    assert stored_text.endswith("…")


def test_item_union_across_turns_same_card(tmp_path):
    # Turn 1: card-1 selected with item i1 for one question. Turn 2: card-1 selected AGAIN
    # (a later turn) with a DIFFERENT item i2 for a different question. On load, the card's items
    # must be the UNION {i1, i2}, not just turn 2's i2 -- item memory survives across turns.
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-1", [_card_with_items(
        "card-1", [{"id": "i1", "type": "note", "locator": {}, "text": "about billing"}])],
        "billing question")
    store.record("conv-1", [_card_with_items(
        "card-1", [{"id": "i2", "type": "note", "locator": {}, "text": "about refunds"}])],
        "refund question")

    loaded = store.load("conv-1")
    assert len(loaded) == 1
    ids = {it["id"] for it in loaded[0]["items"]}
    assert ids == {"i1", "i2"}
    # Card-level fields (title/ts/turn_index) come from the NEWEST occurrence.
    assert loaded[0]["turn_index"] == 0


def test_item_union_keeps_newest_ts_and_unions_keywords(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record("conv-1", [_card_with_items(
        "card-1", [{"id": "i1", "type": "note", "locator": {}, "text": "v1"}])], "billing alpha")
    store.record("conv-1", [_card_with_items(
        "card-1", [{"id": "i1", "type": "note", "locator": {}, "text": "v2"}])], "billing beta")

    loaded = store.load("conv-1")
    item = loaded[0]["items"][0]
    # The newer occurrence's own preview/ts wins...
    assert item["preview"] == "v2"
    # ...but the keywords from BOTH turns are unioned.
    assert "alpha" in item["input_keywords"]
    assert "beta" in item["input_keywords"]


# ---------------------------------------------------------------------------
# Scoped record/load: conv/quest/global, precedence, and per-scope caps.
# ---------------------------------------------------------------------------


def test_record_writes_the_same_turn_to_every_scope_key(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    card = {"id": "card-1", "title": "Shared card"}
    store.record([conv_scope_key("c1"), quest_scope_key("q1"), "global"], [card], "shared question")

    assert store.load(conv_scope_key("c1"))
    assert store.load(quest_scope_key("q1"))
    assert store.load("global")


def test_load_merges_scopes_deduped_by_card_id(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(conv_scope_key("c1"), [{"id": "a", "title": "In conv"}], "q")
    store.record(quest_scope_key("q1"), [{"id": "b", "title": "In quest"}], "q")
    store.record("global", [{"id": "c", "title": "In global"}], "q")

    merged = store.load([conv_scope_key("c1"), quest_scope_key("q1"), "global"])
    ids = {r["id"] for r in merged}
    assert ids == {"a", "b", "c"}


def test_scope_precedence_conv_beats_quest_beats_global_for_same_card_id(tmp_path):
    store = FileRecentContextStore(root_dir=str(tmp_path))
    store.record(conv_scope_key("c1"), [{"id": "shared", "title": "Conv version"}], "q")
    store.record(quest_scope_key("q1"), [{"id": "shared", "title": "Quest version"}], "q")
    store.record("global", [{"id": "shared", "title": "Global version"}], "q")

    merged = store.load([conv_scope_key("c1"), quest_scope_key("q1"), "global"])
    assert len(merged) == 1
    assert merged[0]["title"] == "Conv version"
    assert merged[0]["scope"] == "conv"

    # Without the conv key: quest beats global.
    merged2 = store.load([quest_scope_key("q1"), "global"])
    assert len(merged2) == 1
    assert merged2[0]["title"] == "Quest version"
    assert merged2[0]["scope"] == "quest"


def test_global_scope_uses_its_own_larger_caps(tmp_path):
    # Override BOTH tiers with small, clearly-different caps so the test is fast and precise:
    # conv/quest keep max_turns=1, global gets max_turns=2 -- confirms global uses its OWN cap,
    # not the conv/quest one.
    store = FileRecentContextStore(root_dir=str(tmp_path), max_turns=1, global_max_turns=2)
    store.record("global", [{"id": "a", "title": "First"}], "q1")
    store.record("global", [{"id": "b", "title": "Second"}], "q2")
    # A 3rd turn would push the 1st out under global_max_turns=2.
    store.record("global", [{"id": "c", "title": "Third"}], "q3")

    ids = {r["id"] for r in store.load("global")}
    assert ids == {"b", "c"}  # only the last 2 turns survive (global_max_turns=2)

    # A conv-scope key with the SAME store still only keeps 1 turn (max_turns=1).
    store.record(conv_scope_key("c1"), [{"id": "x", "title": "X"}], "q1")
    store.record(conv_scope_key("c1"), [{"id": "y", "title": "Y"}], "q2")
    ids_conv = {r["id"] for r in store.load(conv_scope_key("c1"))}
    assert ids_conv == {"y"}


def test_global_scope_has_its_own_longer_ttl(tmp_path):
    store = FileRecentContextStore(
        root_dir=str(tmp_path), max_record_age_days=1.0, global_max_record_age_days=20.0)
    old_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
    ).isoformat().replace("+00:00", "Z")
    _seed_raw_file(tmp_path, "global", [
        {"ts": old_ts, "user_text": "old", "cards": [{"id": "old-global", "title": "Old"}]},
    ])
    _seed_raw_file(tmp_path, conv_scope_key("c1"), [
        {"ts": old_ts, "user_text": "old", "cards": [{"id": "old-conv", "title": "Old"}]},
    ])

    # Any record() re-loads + prunes past the applicable TTL for THAT scope.
    store.record("global", [{"id": "new-global", "title": "New"}], "fresh")
    store.record(conv_scope_key("c1"), [{"id": "new-conv", "title": "New"}], "fresh")

    global_ids = {r["id"] for r in store.load("global")}
    conv_ids = {r["id"] for r in store.load(conv_scope_key("c1"))}
    assert global_ids == {"old-global", "new-global"}  # 10 days old survives a 20-day TTL
    assert conv_ids == {"new-conv"}  # 10 days old does NOT survive a 1-day TTL


# ---------------------------------------------------------------------------
# filter_relevant
# ---------------------------------------------------------------------------


def test_followup_forces_only_last_turn_cards():
    records = [
        {"id": "a", "turn_index": 0, "scope": "conv", "title": "unrelated topic zero", "keywords": ["zzzcompletely", "unrelated"]},
        {"id": "b", "turn_index": 1, "scope": "conv", "title": "unrelated topic one", "keywords": ["zzzcompletely", "unrelated"]},
    ]
    result = filter_relevant(records, "totally different subject entirely", is_followup=True, max_cards=6)
    ids = {r["id"] for r in result}
    assert ids == {"a"}  # turn_index 0 forced through; the older turn has no overlap so it is dropped


def test_followup_older_turn_still_needs_and_gets_overlap():
    records = [
        {"id": "a", "turn_index": 0, "scope": "conv", "title": "billing questions", "keywords": ["billing", "invoice"]},
        {"id": "b", "turn_index": 1, "scope": "conv", "title": "refund policy details", "keywords": ["refund", "policy"]},
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


# --- scope weighting: conv > quest > global, and NO free pass outside conv --------------------


def test_quest_scope_gets_no_free_pass_even_on_turn_index_zero():
    # A quest-scope record at turn_index 0 with ZERO lexical overlap must NOT pass on a follow-up
    # input -- only conv-scope gets the free pass.
    records = [{"id": "a", "turn_index": 0, "scope": "quest", "title": "zzz unrelated", "keywords": ["zzz", "unrelated"]}]
    result = filter_relevant(records, "totally different subject", is_followup=True, max_cards=6)
    assert result == []


def test_global_scope_gets_no_free_pass_even_on_turn_index_zero():
    records = [{"id": "a", "turn_index": 0, "scope": "global", "title": "zzz unrelated", "keywords": ["zzz", "unrelated"]}]
    result = filter_relevant(records, "totally different subject", is_followup=True, max_cards=6)
    assert result == []


def test_conv_scope_still_gets_the_free_pass_on_followup():
    records = [{"id": "a", "turn_index": 0, "scope": "conv", "title": "zzz unrelated", "keywords": ["zzz", "unrelated"]}]
    result = filter_relevant(records, "totally different subject", is_followup=True, max_cards=6)
    assert [r["id"] for r in result] == ["a"]


def test_conv_scope_outranks_quest_and_global_at_equal_lexical_relevance():
    # All three records have IDENTICAL keywords/title (same lexical relevance to the query) and the
    # same recency (no ts -> same recency weight); only the scope differs. conv (weight 1.0) must
    # rank above quest (0.8), which must rank above global (0.5).
    shared_kw = ["billing", "invoice"]
    records = [
        {"id": "g", "turn_index": 5, "scope": "global", "title": "billing", "keywords": shared_kw},
        {"id": "q", "turn_index": 5, "scope": "quest", "title": "billing", "keywords": shared_kw},
        {"id": "c", "turn_index": 5, "scope": "conv", "title": "billing", "keywords": shared_kw},
    ]
    result = filter_relevant(records, "billing invoice question", is_followup=False, max_cards=6)
    assert [r["id"] for r in result] == ["c", "q", "g"]


def test_unstamped_scope_defaults_to_conv_weight_and_forced_pass():
    # A record with no "scope" key at all (as bare-key/legacy callers produce) behaves exactly like
    # a conv-scope record: full weight, and the follow-up free pass applies.
    records = [{"id": "a", "turn_index": 0, "title": "zzz unrelated", "keywords": ["zzz"]}]
    result = filter_relevant(records, "totally different subject", is_followup=True, max_cards=6)
    assert [r["id"] for r in result] == ["a"]


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


def test_render_recent_cards_ranks_matching_item_first():
    # Two items on the same card: i2's stored input_keywords overlap the CURRENT query; i1's don't.
    # i2 must render before i1 regardless of stored order.
    records = [{
        "id": "card-1",
        "title": "Card",
        "items": [
            {"id": "i1", "preview": "about billing", "input_keywords": ["billing"], "last_used_ts": ""},
            {"id": "i2", "preview": "about refunds", "input_keywords": ["refund"], "last_used_ts": ""},
        ],
    }]
    text, entries = render_recent_cards(records, "refund question")
    assert text.index("about refunds") < text.index("about billing")
    assert entries[0]["items"][0]["id"] in {"i1", "i2"}  # items list itself is unchanged (raw)


def test_render_recent_cards_ties_break_by_recency():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=30)).isoformat().replace("+00:00", "Z")
    records = [{
        "id": "card-1",
        "title": "Card",
        "items": [
            {"id": "old", "preview": "old item", "input_keywords": [], "last_used_ts": old},
            {"id": "new", "preview": "new item", "input_keywords": [], "last_used_ts": now},
        ],
    }]
    text, _entries = render_recent_cards(records, "unrelated query with no overlap")
    assert text.index("new item") < text.index("old item")


# ---------------------------------------------------------------------------
# build_item_usage_hint
# ---------------------------------------------------------------------------


def test_build_item_usage_hint_ranks_by_overlap_then_recency():
    records = [{
        "id": "card-1",
        "items": [
            {"id": "i1", "input_keywords": ["billing"], "last_used_ts": ""},
            {"id": "i2", "input_keywords": ["refund"], "last_used_ts": ""},
        ],
    }]
    hint = build_item_usage_hint(records, "refund question")
    assert hint == {"card-1": ["i2", "i1"]}


def test_build_item_usage_hint_skips_cards_without_items():
    records = [{"id": "card-1", "items": []}, {"id": "card-2"}]
    assert build_item_usage_hint(records, "anything") == {}


def test_build_item_usage_hint_empty_records():
    assert build_item_usage_hint([], "anything") == {}


def test_build_item_usage_hint_caps_items_per_card():
    items = [{"id": f"i{i}", "input_keywords": [], "last_used_ts": ""} for i in range(12)]
    hint = build_item_usage_hint([{"id": "card-1", "items": items}], "q", max_items_per_card=3)
    assert len(hint["card-1"]) == 3
