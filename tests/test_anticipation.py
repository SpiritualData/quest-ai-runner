"""Offline tests for the ANTICIPATION ENGINE (see core/anticipation.py): the shared pure functions
(feature extraction, similarity, the objective function, the EMA update, pattern reinforcement/
pruning, ranking, prediction generation/matching), the runner-lane ``FilePredictionStore`` +
``Anticipator`` wrapper, and its wiring into the ``Orchestrator`` (opt-in, byte-for-byte inert when
off) and ``config.resolve_anticipator``.

Follows the conventions of ``test_orchestrator_recent_context.py`` (same Stub* doubles from
``conftest.py``, ``FilePredictionStore`` rooted at ``tmp_path`` the way ``FileRecentContextStore``
is) and ``test_background_index_shutdown.py`` (joining a background thread via
``config.shutdown_background_index()`` before asserting on what it wrote).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from quest_ai_runner.config import resolve_anticipator, shutdown_background_index, RunnerConfig
from quest_ai_runner.core.anticipation import (
    CONF_FLOOR,
    K,
    MATCH_SERVE,
    MAX_FOLLOWUPS,
    MAX_PATTERNS_PER_SCOPE,
    PREDICTION_TTL_SECONDS,
    PRUNE_AGE_DAYS,
    PRUNE_WEIGHT,
    AskFeatures,
    Anticipator,
    FilePredictionStore,
    Pattern,
    Prediction,
    apply_refresh,
    chips_for_now,
    extract_features,
    generate_predictions,
    match_actual,
    parse_refresh_response,
    rank_patterns,
    reinforce_or_create,
    score_outcome,
    similarity,
    update_weight,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval


# =================================================================================================
# extract_features
# =================================================================================================


def test_extract_features_hour_bucket_edges():
    # Bucket edges: (6, 10, 14, 17, 21, 24) -> 0=00-05, 1=06-09, 2=10-13, 3=14-16, 4=17-20, 5=21-23.
    cases = [
        (0, 0), (5, 0),
        (6, 1), (9, 1),
        (10, 2), (13, 2),
        (14, 3), (16, 3),
        (17, 4), (20, 4),
        (21, 5), (23, 5),
    ]
    for hour, expected_bucket in cases:
        now = datetime(2026, 7, 20, hour, 0, 0)  # a Monday
        feats = extract_features("hello world", now, "global")
        assert feats.hour_bucket == expected_bucket, f"hour={hour}"


def test_extract_features_weekday_and_weekend():
    monday = datetime(2026, 7, 20, 9, 0, 0)
    saturday = datetime(2026, 7, 25, 9, 0, 0)
    sunday = datetime(2026, 7, 26, 9, 0, 0)

    feats_mon = extract_features("ask", monday, "global")
    assert feats_mon.dow == 0
    assert feats_mon.is_weekend is False

    feats_sat = extract_features("ask", saturday, "global")
    assert feats_sat.dow == 5
    assert feats_sat.is_weekend is True

    feats_sun = extract_features("ask", sunday, "global")
    assert feats_sun.dow == 6
    assert feats_sun.is_weekend is True


def test_extract_features_empty_text_gives_no_keywords():
    now = datetime(2026, 7, 20, 9, 0, 0)
    feats = extract_features("", now, "conv:abc")
    assert feats.keywords == []
    assert feats.scope == "conv:abc"


def test_extract_features_keywords_filter_stopwords_and_short_tokens():
    now = datetime(2026, 7, 20, 9, 0, 0)
    feats = extract_features("what is the roadmap for our pricing plan", now, "global")
    assert "roadmap" in feats.keywords
    assert "pricing" in feats.keywords
    # Stopwords / too-short tokens are filtered.
    assert "the" not in feats.keywords
    assert "for" not in feats.keywords
    assert "is" not in feats.keywords


# =================================================================================================
# similarity
# =================================================================================================


def test_similarity_empty_sets_zero():
    assert similarity([], []) == 0.0
    assert similarity(["roadmap"], []) == 0.0
    assert similarity([], ["roadmap"]) == 0.0


def test_similarity_identical_sets_is_one():
    kws = ["roadmap", "pricing", "plan"]
    assert similarity(kws, list(kws)) == 1.0


def test_similarity_short_subset_scores_high_containment():
    long_set = ["roadmap", "pricing", "plan", "quarterly", "review", "meeting"]
    short_subset = ["roadmap", "pricing"]
    score = similarity(short_subset, long_set)
    # containment = 2/2 = 1.0; jaccard = 2/6 = 0.333...; 0.5*1.0 + 0.5*0.333 ~= 0.667
    assert score > 0.6
    assert score < 1.0


def test_similarity_disjoint_sets_zero():
    assert similarity(["roadmap", "pricing"], ["weather", "sports"]) == 0.0


# =================================================================================================
# score_outcome and update_weight
# =================================================================================================


def test_score_outcome_matches_similarity_of_keywords():
    assert score_outcome("what is our roadmap", "what is our roadmap") == 1.0
    assert score_outcome("what is our roadmap", "how's the weather today") == 0.0


def test_score_outcome_empty_text_scores_zero():
    assert score_outcome("", "what is our roadmap") == 0.0
    assert score_outcome("what is our roadmap", "") == 0.0
    assert score_outcome("", "") == 0.0


def test_update_weight_ema_formula():
    assert update_weight(0.5, 1.0, alpha=0.3) == 0.5 + 0.3 * (1.0 - 0.5)
    assert update_weight(0.5, 0.0, alpha=0.3) == 0.5 + 0.3 * (0.0 - 0.5)


def test_update_weight_converges_toward_one_on_repeated_hits():
    w = CONF_FLOOR
    for _ in range(30):
        w = update_weight(w, 1.0)
    assert w > 0.99


def test_update_weight_decays_toward_zero_and_crosses_prune_weight_on_misses():
    w = 0.8
    crossed = False
    for _ in range(30):
        w = update_weight(w, 0.0)
        if w < PRUNE_WEIGHT:
            crossed = True
            break
    assert crossed, "a weight that keeps missing must eventually cross PRUNE_WEIGHT"
    assert w < 0.05


# =================================================================================================
# reinforce_or_create
# =================================================================================================


def _pattern(pattern_id="p1", scope="global", canonical_text="what is our roadmap",
             keywords=None, weight=0.5, hits=0, misses=0, last_seen_ts=1000.0,
             created_ts=1000.0, hour_bucket=1, dow=0, is_weekend=False) -> Pattern:
    return Pattern(
        pattern_id=pattern_id, scope=scope, hour_bucket=hour_bucket, dow=dow,
        is_weekend=is_weekend, canonical_text=canonical_text,
        keywords=keywords if keywords is not None else ["roadmap", "our"],
        weight=weight, hits=hits, misses=misses,
        last_seen_ts=last_seen_ts, created_ts=created_ts,
    )


def _features(scope="global", text="what is our roadmap", hour=9, dow=0) -> AskFeatures:
    """Build real ``AskFeatures`` via ``extract_features`` for a given hour/weekday, anchored on
    2026-07-20 (a Monday, dow=0)."""
    from datetime import timedelta
    anchor_monday = datetime(2026, 7, 20, hour, 0, 0)
    now = anchor_monday + timedelta(days=dow)
    return extract_features(text, now, scope)


def test_reinforce_or_create_reinforces_above_sim_reinforce():
    # Default keywords ["roadmap", "our"] overlap heavily with the new ask's keywords
    # (["our", "roadmap", "plan"]): sim ~0.83, well above SIM_REINFORCE (0.55).
    p = _pattern(weight=0.5, hits=2)
    feats = _features(text="what is our roadmap plan", hour=9, dow=2)  # new day/hour signature
    patterns, absorbed = reinforce_or_create([p], feats, "what is our roadmap plan", now_ts=2000.0)

    assert absorbed.pattern_id == p.pattern_id  # same pattern, reinforced not replaced
    assert absorbed.hits == 3
    assert absorbed.weight > 0.5  # EMA moved toward 1.0
    assert absorbed.weight == update_weight(0.5, 1.0)
    assert set(absorbed.keywords) >= {"roadmap", "our"}  # merged, original kept
    assert "plan" in absorbed.keywords  # new keyword folded in
    assert absorbed.dow == 2  # time signature moved to the latest occurrence
    assert len(patterns) == 1
    assert patterns[0] is absorbed


def test_reinforce_or_create_below_threshold_creates_new_pattern():
    p = _pattern(canonical_text="what is our roadmap", keywords=["roadmap"])
    feats = _features(text="how do I reset my password", hour=9, dow=0)
    patterns, absorbed = reinforce_or_create([p], feats, "how do I reset my password", now_ts=2000.0)

    assert absorbed.pattern_id != p.pattern_id
    assert absorbed.canonical_text == "how do I reset my password"
    assert absorbed.weight == CONF_FLOOR
    assert absorbed.hits == 1
    assert len(patterns) == 2
    assert p in patterns
    assert absorbed in patterns


def test_reinforce_or_create_prunes_low_weight_patterns():
    low = _pattern(pattern_id="low", weight=0.01, canonical_text="unrelated old ask",
                   keywords=["unrelated"], last_seen_ts=1000.0)
    feats = _features(text="brand new different topic", hour=9, dow=0)
    patterns, absorbed = reinforce_or_create([low], feats, "brand new different topic", now_ts=2000.0)

    assert low not in patterns  # pruned: below PRUNE_WEIGHT
    assert absorbed in patterns


def test_reinforce_or_create_prunes_by_age():
    max_age_seconds = PRUNE_AGE_DAYS * 86400.0
    now_ts = 10_000_000.0
    old = _pattern(pattern_id="old", weight=0.9, canonical_text="an old stale ask",
                   keywords=["stale"], last_seen_ts=now_ts - max_age_seconds - 1.0)
    feats = _features(text="brand new topic entirely", hour=9, dow=0)
    patterns, absorbed = reinforce_or_create([old], feats, "brand new topic entirely", now_ts=now_ts)

    assert old not in patterns  # pruned: too old, even with a high weight
    assert absorbed in patterns


def test_reinforce_or_create_absorbed_pattern_never_pruned_even_if_it_would_qualify():
    # A brand-new pattern always starts at CONF_FLOOR (well above PRUNE_WEIGHT) so this mostly
    # documents the "absorbed is always kept" contract directly.
    feats = _features(text="totally fresh ask", hour=9, dow=0)
    patterns, absorbed = reinforce_or_create([], feats, "totally fresh ask", now_ts=1.0)
    assert absorbed in patterns
    assert len(patterns) == 1


def test_reinforce_or_create_max_patterns_per_scope_cap_keeps_absorbed():
    many = [
        _pattern(pattern_id=f"p{i}", weight=0.5 + (i * 0.0001), canonical_text=f"ask number {i}",
                keywords=[f"topic{i}"], last_seen_ts=1000.0 + i)
        for i in range(MAX_PATTERNS_PER_SCOPE)
    ]
    feats = _features(text="a totally new unrelated ask", hour=9, dow=0)
    patterns, absorbed = reinforce_or_create(many, feats, "a totally new unrelated ask", now_ts=2000.0)

    assert len(patterns) == MAX_PATTERNS_PER_SCOPE  # capped
    assert absorbed in patterns  # the just-absorbed pattern is always kept


def test_reinforce_or_create_does_not_mutate_input_list():
    p = _pattern(weight=0.5, hits=2)
    original = list([p])
    feats = _features(text="what is our roadmap", hour=9, dow=0)
    reinforce_or_create(original, feats, "what is our roadmap", now_ts=2000.0)

    assert original == [p]  # the caller's list and its Pattern objects are untouched
    assert original[0].weight == 0.5
    assert original[0].hits == 2


# =================================================================================================
# rank_patterns
# =================================================================================================


def test_rank_patterns_filters_by_scope():
    p_a = _pattern(pattern_id="a", scope="conv:1", weight=0.9)
    p_b = _pattern(pattern_id="b", scope="conv:2", weight=0.9)
    feats = _features(scope="conv:1", text="what is our roadmap", hour=9, dow=0)

    ranked = rank_patterns([p_a, p_b], feats)
    assert [p.pattern_id for p, _ in ranked] == ["a"]


def test_rank_patterns_time_proximity_orders_closer_pattern_first():
    same_time = _pattern(pattern_id="same", hour_bucket=1, dow=0, weight=0.5, keywords=[])
    far_time = _pattern(pattern_id="far", hour_bucket=4, dow=3, weight=0.5, keywords=[])
    feats = AskFeatures(hour_bucket=1, dow=0, is_weekend=False, keywords=[], scope="global")

    ranked = rank_patterns([far_time, same_time], feats)
    assert [p.pattern_id for p, _ in ranked] == ["same", "far"]


def test_rank_patterns_keyword_factor_floors_at_half_for_unrelated_topic():
    p = _pattern(hour_bucket=1, dow=0, weight=1.0, keywords=["roadmap"])
    feats = AskFeatures(hour_bucket=1, dow=0, is_weekend=False,
                        keywords=["completely", "unrelated", "topic"], scope="global")

    ranked = rank_patterns([p], feats)
    assert len(ranked) == 1
    _, score = ranked[0]
    # time_proximity = 1.0 (same bucket/day), kw_factor floors at 0.5 (no overlap), weight = 1.0
    assert score == 0.5


def test_rank_patterns_no_topic_keywords_gives_full_keyword_factor():
    p = _pattern(hour_bucket=1, dow=0, weight=1.0, keywords=["roadmap"])
    feats = AskFeatures(hour_bucket=1, dow=0, is_weekend=False, keywords=[], scope="global")

    ranked = rank_patterns([p], feats)
    _, score = ranked[0]
    assert score == 1.0  # time_proximity 1.0 * kw_factor 1.0 (no topic keywords) * weight 1.0


# =================================================================================================
# generate_predictions
# =================================================================================================


def test_generate_predictions_respects_confidence_floor():
    weak = _pattern(pattern_id="weak", canonical_text="weak ask", keywords=["weak"], weight=0.01)
    feats = _features(text="weak ask", hour=9, dow=0)
    preds = generate_predictions([weak], feats, recent_texts=[])
    assert preds == []  # score well below CONF_FLOOR -> nothing generated


def test_generate_predictions_dedupes_by_canonical_text():
    p1 = _pattern(pattern_id="p1", canonical_text="what is our roadmap",
                  keywords=["roadmap"], weight=0.9)
    p2 = _pattern(pattern_id="p2", canonical_text="what is our roadmap",
                  keywords=["roadmap"], weight=0.8)
    feats = _features(text="what is our roadmap", hour=9, dow=0)
    preds = generate_predictions([p1, p2], feats, recent_texts=[])
    texts = [p.text for p in preds]
    assert texts.count("what is our roadmap") == 1


def test_generate_predictions_respects_k_cap():
    patterns = [
        _pattern(pattern_id=f"p{i}", canonical_text=f"ask topic {i}", keywords=[f"topic{i}"],
                weight=0.9)
        for i in range(K + 3)
    ]
    feats = AskFeatures(hour_bucket=1, dow=0, is_weekend=False, keywords=[], scope="global")
    preds = generate_predictions(patterns, feats, recent_texts=[], k=K)
    assert len(preds) == K


def test_generate_predictions_sets_ttl():
    p = _pattern(canonical_text="what is our roadmap", keywords=["roadmap"], weight=0.9)
    feats = _features(text="what is our roadmap", hour=9, dow=0)
    preds = generate_predictions([p], feats, recent_texts=[])
    assert len(preds) == 1
    pred = preds[0]
    assert pred.expires_ts > pred.created_ts
    assert pred.source == "pattern"
    assert pred.text == "what is our roadmap"


# =================================================================================================
# match_actual
# =================================================================================================


def test_match_actual_serves_only_at_or_above_match_serve():
    strong = Prediction(prediction_id="strong", text="what is our roadmap", confidence=0.9)
    weak = Prediction(prediction_id="weak", text="totally different subject", confidence=0.9)

    best, score, outcomes = match_actual([strong, weak], "what is our roadmap")
    assert best is not None
    assert best.prediction_id == "strong"
    assert score >= MATCH_SERVE
    assert {pid for pid, _ in outcomes} == {"strong", "weak"}  # both scored


def test_match_actual_below_match_serve_serves_nothing_but_still_scores():
    weak = Prediction(prediction_id="weak", text="completely unrelated ask", confidence=0.9)
    best, score, outcomes = match_actual([weak], "another entirely different message")
    assert best is None
    assert score == 0.0
    assert outcomes == [("weak", 0.0)]


def test_match_actual_records_outcome_for_every_prediction_including_misses():
    hit = Prediction(prediction_id="hit", text="what is our roadmap", confidence=0.9)
    miss = Prediction(prediction_id="miss", text="unrelated weather question", confidence=0.9)
    best, score, outcomes = match_actual([hit, miss], "what is our roadmap")
    outcome_map = dict(outcomes)
    assert outcome_map["hit"] > 0.0
    assert outcome_map["miss"] == 0.0


def test_match_actual_empty_predictions():
    best, score, outcomes = match_actual([], "anything")
    assert best is None
    assert score == 0.0
    assert outcomes == []


# =================================================================================================
# FilePredictionStore
# =================================================================================================


def test_file_prediction_store_patterns_round_trip(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    p = _pattern(pattern_id="p1", scope="global")
    store.save_patterns("global", [p])

    loaded = store.load_patterns("global")
    assert len(loaded) == 1
    assert loaded[0].pattern_id == "p1"
    assert loaded[0].canonical_text == p.canonical_text
    assert loaded[0].keywords == p.keywords


def test_file_prediction_store_predictions_round_trip(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])

    loaded = store.load_predictions("global")
    assert len(loaded) == 1
    assert loaded[0].prediction_id == "pr1"
    assert loaded[0].text == "what is our roadmap"


def time_far_future() -> float:
    import time
    return time.time() + 3600.0


def time_past() -> float:
    import time
    return time.time() - 3600.0


def test_file_prediction_store_drops_expired_predictions_on_load(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    live = Prediction(prediction_id="live", text="a", confidence=0.5,
                      created_ts=1000.0, expires_ts=time_far_future())
    expired = Prediction(prediction_id="expired", text="b", confidence=0.5,
                         created_ts=1000.0, expires_ts=time_past())
    store.save_predictions("global", [live, expired])

    loaded = store.load_predictions("global")
    assert {p.prediction_id for p in loaded} == {"live"}


def test_file_prediction_store_context_view_sidecar_round_trip(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred], views={"pr1": "precomputed bundle text"})

    view = store.load_view("global", "pr1")
    assert view == "precomputed bundle text"
    # A prediction with no view stored returns "".
    assert store.load_view("global", "missing") == ""


def test_file_prediction_store_clear_predictions_keeps_patterns(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    p = _pattern()
    pred = Prediction(prediction_id="pr1", text="a", confidence=0.5,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_patterns("global", [p])
    store.save_predictions("global", [pred])

    store.clear_predictions("global")

    assert store.load_predictions("global") == []
    assert len(store.load_patterns("global")) == 1


def test_file_prediction_store_append_log_writes_jsonl_lines(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    store.append_log({"event": "outcome", "score": 0.5})
    store.append_log({"event": "outcome", "score": 0.9})

    lines = store.log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["score"] == 0.5
    assert records[1]["score"] == 0.9


def test_file_prediction_store_corrupt_file_returns_empty_without_raising(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    path = store._path("global")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json{{{", encoding="utf-8")

    assert store.load_patterns("global") == []
    assert store.load_predictions("global") == []
    assert store.load_view("global", "anything") == ""
    # And a subsequent write still works (does not raise, does not depend on the corrupt state).
    store.save_patterns("global", [_pattern()])
    assert len(store.load_patterns("global")) == 1


# =================================================================================================
# Anticipator end-to-end (stub assembler, FilePredictionStore rooted at tmp_path)
# =================================================================================================


class _StubAssembledContext:
    def __init__(self, context_view: str, card_ids: Optional[List[str]] = None):
        self.context_view = context_view
        self.card_ids = card_ids or []


class _StubAssembler:
    """A minimal ContextAssembler-shaped stub: assemble(text) -> object with context_view/card_ids."""

    def __init__(self):
        self.calls: List[str] = []

    def assemble(self, text: str) -> _StubAssembledContext:
        self.calls.append(text)
        return _StubAssembledContext(context_view=f"BUNDLE FOR: {text}", card_ids=[f"card-{text[:8]}"])


def test_anticipator_plan_next_persists_predictions_with_precomputed_views(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    assembler = _StubAssembler()
    anticipator = Anticipator(store, assembler=assembler)

    # Seed a pattern directly (bypassing learn(), which needs an actual turn) so plan_next has
    # something to rank.
    now = datetime(2026, 7, 20, 9, 0, 0)
    p = _pattern(scope="global", canonical_text="what is our roadmap", keywords=["roadmap"],
                weight=0.9, hour_bucket=1, dow=0)
    store.save_patterns("global", [p])

    planned = anticipator.plan_next("global", recent_texts=["what is our roadmap"], now=now)

    assert len(planned) == 1
    assert planned[0].text == "what is our roadmap"
    live = store.load_predictions("global")
    assert len(live) == 1
    view = store.load_view("global", live[0].prediction_id)
    assert view == "BUNDLE FOR: what is our roadmap"


def test_anticipator_observe_matches_and_returns_precomputed_context(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future(),
                      context_card_ids=["card-1"])
    store.save_predictions("global", [pred], views={"pr1": "precomputed bundle"})

    result = anticipator.observe("what is our roadmap", "global")

    assert result.matched is not None
    assert result.matched.prediction_id == "pr1"
    assert result.score >= MATCH_SERVE
    assert result.precomputed is not None
    assert result.precomputed.context_view == "precomputed bundle"
    assert result.precomputed.card_ids == ["card-1"]
    # Live predictions were consumed (judged this turn).
    assert store.load_predictions("global") == []


def test_anticipator_observe_logs_outcome_for_matched_prediction(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])

    anticipator.observe("what is our roadmap", "global")

    lines = store.log_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 1
    assert records[0]["prediction_id"] == "pr1"
    assert records[0]["matched"] is True


def test_anticipator_observe_applies_ema_hit_to_source_pattern(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    p = _pattern(pattern_id="src", scope="global", canonical_text="what is our roadmap",
                weight=0.5, hits=0, misses=0)
    store.save_patterns("global", [p])
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])

    anticipator.observe("what is our roadmap", "global")

    updated = store.load_patterns("global")
    assert len(updated) == 1
    assert updated[0].weight > 0.5  # EMA hit moved weight up
    assert updated[0].hits == 1
    assert updated[0].misses == 0


def test_anticipator_observe_nonmatch_returns_empty_but_logs_miss_and_decays(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    p = _pattern(pattern_id="src", scope="global", canonical_text="what is our roadmap",
                weight=0.5, hits=0, misses=0)
    store.save_patterns("global", [p])
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])

    result = anticipator.observe("a completely unrelated weather question", "global")

    assert result.matched is None
    assert result.precomputed is None
    assert result.score == 0.0

    lines = store.log_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 1
    assert records[0]["matched"] is False

    updated = store.load_patterns("global")
    assert updated[0].weight < 0.5  # EMA miss decayed weight down
    assert updated[0].misses == 1
    assert updated[0].hits == 0


def test_anticipator_observe_clears_live_predictions_after_judging(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])

    anticipator.observe("anything at all", "global")

    assert store.load_predictions("global") == []


def test_anticipator_learn_creates_then_reinforces_a_pattern():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = FilePredictionStore(tmp)
        anticipator = Anticipator(store, assembler=None)
        now = datetime(2026, 7, 20, 9, 0, 0)

        anticipator.learn("what is our roadmap", "global", now=now)
        first = store.load_patterns("global")
        assert len(first) == 1
        assert first[0].hits == 1
        assert first[0].weight == CONF_FLOOR

        anticipator.learn("what is our roadmap plan", "global", now=now)
        second = store.load_patterns("global")
        assert len(second) == 1  # reinforced the same pattern, not a second one
        assert second[0].hits == 2
        assert second[0].weight > CONF_FLOOR


def test_anticipator_close_stops_plan_next_between_bundles(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    assembler = _StubAssembler()
    anticipator = Anticipator(store, assembler=assembler)
    now = datetime(2026, 7, 20, 9, 0, 0)
    patterns = [
        _pattern(pattern_id=f"p{i}", scope="global", canonical_text=f"ask {i}",
                keywords=[f"topic{i}"], weight=0.9, hour_bucket=1, dow=0)
        for i in range(K)
    ]
    store.save_patterns("global", patterns)
    anticipator.close()

    planned = anticipator.plan_next("global", recent_texts=[], now=now)

    assert planned == []  # closed before the first scope's bundle -> nothing planned
    assert assembler.calls == []  # precompute never ran


# =================================================================================================
# Orchestrator integration
# =================================================================================================


def _one_answer_provider() -> StubProvider:
    return StubProvider(decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}])


class _QuietProvider:
    """A minimal ModelProvider (same plan()/answer()/list_models() shape as ``StubProvider``)
    whose ``answer()`` returns an EMPTY string.

    Used only for the anticipation end-to-end test below: ``StubProvider.answer()`` always appends
    a fixed ``" [grounded_on:...]"`` suffix, which is extra TOPIC-KEYWORD NOISE that
    ``Anticipator.plan_next`` folds into its ranking (``_kickoff_anticipation`` feeds the turn's own
    reply text into ``recent_texts``). That noise dilutes the jaccard half of ``rank_patterns``'
    keyword factor enough that a just-created pattern (starting at ``CONF_FLOOR``) never clears the
    confidence floor after a single turn -- worth knowing about the real implementation (see the
    report), and worked around here with a provider that contributes zero extra keywords.
    """

    def __init__(self, decisions: List[Dict[str, Any]]):
        self._decisions = list(decisions)
        self.plan_calls = 0
        self.plan_prompts: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.plan_calls += 1
        self.plan_prompts.append(prompt)
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None) -> str:
        return ""

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


def test_anticipation_disabled_by_default_never_touches_store_dir(tmp_path):
    """The inertness guarantee: with the flag OFF (the default) but an Anticipator wired, a run
    never creates the predictions directory at all."""
    store_root = tmp_path / "context-root"
    store = FilePredictionStore(str(store_root))
    assembler = _StubAssembler()
    anticipator = Anticipator(store, assembler=assembler)
    provider = _one_answer_provider()

    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        anticipator=anticipator,
        config=OrchestratorConfig(anticipation_enabled=False),
    )

    res = orch.run("what is our roadmap", quest_id="quest-1")
    shutdown_background_index(timeout=5.0)

    assert res.kind == "answer"
    assert not store._dir.exists(), "flag off must touch zero store files, not just skip serving"
    assert assembler.calls == []


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` (a zero-arg callable) until it's truthy or ``timeout`` elapses.

    ``_kickoff_anticipation``'s background thread starts asynchronously (``threading.Thread.start()``
    returns before the OS necessarily schedules it), and ``shutdown_background_index()`` calls
    ``close()`` on the anticipator SYNCHRONOUSLY before joining -- calling it immediately after
    ``run()`` returns can race ahead of the thread even beginning its ``plan_next`` loop (``close()``
    is checked at the top of that loop, so a same-instant close can skip planning entirely even
    though ``learn()`` already completed; see the report). Polling for the actual side effect avoids
    depending on that race resolving one particular way, and is still fast in the common case.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_anticipation_enabled_learns_and_serves_context_on_repeat_ask(tmp_path):
    """End-to-end: turn 1 (flag on, no patterns yet) answers normally and its turn-end background
    thread learns a pattern + plans a precomputed prediction; after joining that thread, turn 2
    asking the SAME thing gets the anticipated context injected into the planner prompt."""
    store = FilePredictionStore(str(tmp_path))
    assembler = _StubAssembler()
    anticipator = Anticipator(store, assembler=assembler)

    provider1 = _QuietProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}])
    orch1 = Orchestrator(
        retrieval=StubRetrieval(), provider=provider1, registry=ModelRegistry(provider1),
        anticipator=anticipator,
        config=OrchestratorConfig(anticipation_enabled=True),
    )
    res1 = orch1.run("what is our roadmap", quest_id="quest-1")
    assert res1.kind == "answer"

    # Turn 1 has no live predictions yet, so nothing was anticipated.
    assert "ANTICIPATED CONTEXT" not in provider1.plan_prompts[0]

    # Wait for the background thread to actually finish planning (see _wait_until), then join it
    # via shutdown_background_index() (a fast no-op by the time the predicate is satisfied).
    assert _wait_until(lambda: len(store.load_predictions("global")) > 0), (
        "the turn-end background thread never planned a prediction")
    shutdown_background_index(timeout=10.0)

    patterns = store.load_patterns("global")
    assert len(patterns) == 1
    assert patterns[0].canonical_text == "what is our roadmap"
    live = store.load_predictions("global")
    assert len(live) == 1
    assert live[0].text == "what is our roadmap"

    # Turn 2: the SAME anticipator/store, a fresh orchestrator (a new turn), asking the same thing.
    provider2 = _QuietProvider(
        decisions=[{"action": "answer", "model_tier": "sonnet", "rationale": "answer"}])
    orch2 = Orchestrator(
        retrieval=StubRetrieval(), provider=provider2, registry=ModelRegistry(provider2),
        anticipator=anticipator,
        config=OrchestratorConfig(anticipation_enabled=True),
    )
    res2 = orch2.run("what is our roadmap", quest_id="quest-1")
    assert res2.kind == "answer"

    assert "ANTICIPATED CONTEXT" in provider2.plan_prompts[0]
    assert "BUNDLE FOR: what is our roadmap" in provider2.plan_prompts[0]

    shutdown_background_index(timeout=10.0)

    # The outcome log recorded a match for this served prediction.
    lines = store.log_path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert any(r["matched"] is True for r in records)


def test_anticipation_flag_off_run_is_byte_for_byte_identical_prompt(tmp_path):
    """Same setup as the enabled test but with the flag OFF: even with patterns/predictions
    already sitting in the store, a run must never surface anticipated context."""
    store = FilePredictionStore(str(tmp_path))
    assembler = _StubAssembler()
    anticipator = Anticipator(store, assembler=assembler)
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred], views={"pr1": "precomputed bundle"})

    provider = _one_answer_provider()
    orch = Orchestrator(
        retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
        anticipator=anticipator,
        config=OrchestratorConfig(anticipation_enabled=False),
    )
    res = orch.run("what is our roadmap", quest_id="quest-1")
    shutdown_background_index(timeout=5.0)

    assert res.kind == "answer"
    assert "ANTICIPATED CONTEXT" not in provider.plan_prompts[0]
    # The store's existing predictions are untouched (observe() never ran).
    assert len(store.load_predictions("global")) == 1


# =================================================================================================
# resolve_anticipator
# =================================================================================================


def test_resolve_anticipator_returns_none_when_flag_off(tmp_path):
    cfg = RunnerConfig(context_cards_dir=str(tmp_path))
    cfg.orchestrator.anticipation_enabled = False
    assert resolve_anticipator(cfg) is None


def test_resolve_anticipator_returns_anticipator_when_flag_on(tmp_path):
    cfg = RunnerConfig(context_cards_dir=str(tmp_path))
    cfg.orchestrator.anticipation_enabled = True
    anticipator = resolve_anticipator(cfg)
    assert isinstance(anticipator, Anticipator)
    assert isinstance(anticipator.store, FilePredictionStore)
    assert anticipator.store._dir == (tmp_path / "predictions")


# =================================================================================================
# v2: TTL semantics, chips_for_now, display_text, exact-id serve, refresh
# =================================================================================================


def test_prediction_ttl_is_four_hours():
    # v2: the TTL now bounds a precomputed BUNDLE's freshness, not chip visibility.
    assert PREDICTION_TTL_SECONDS == 14400


def test_chips_for_now_empty_patterns_returns_empty():
    assert chips_for_now([], datetime(2026, 7, 20, 9, 0, 0), recent_texts=[]) == []


def test_chips_for_now_time_matched_pattern_from_days_ago_still_surfaces():
    # A pattern learned days ago at THIS hour/weekday still produces a chip now (read-time),
    # with no live prediction and no replanning.
    now = datetime(2026, 7, 20, 9, 0, 0)  # Monday, hour bucket 1
    feats = extract_features("what is our roadmap", now, "global")
    old = Pattern(
        pattern_id="old", scope="global", hour_bucket=feats.hour_bucket, dow=feats.dow,
        is_weekend=feats.is_weekend, canonical_text="what is our roadmap",
        keywords=feats.keywords, weight=0.9, hits=5, misses=0,
        last_seen_ts=1000.0, created_ts=1000.0)
    chips = chips_for_now([old], now, recent_texts=[])
    assert [c.text for c in chips] == ["what is our roadmap"]


def test_chips_for_now_orders_time_matched_ahead_of_time_far():
    now = datetime(2026, 7, 20, 9, 0, 0)  # bucket 1, dow 0
    near = _pattern(pattern_id="near", canonical_text="near ask", keywords=["near"],
                    weight=0.9, hour_bucket=1, dow=0)
    far = _pattern(pattern_id="far", canonical_text="far ask", keywords=["far"],
                   weight=0.9, hour_bucket=4, dow=3)
    chips = chips_for_now([near, far], now, recent_texts=[])
    assert chips[0].text == "near ask"


def test_chips_for_now_dedupes_and_caps():
    now = datetime(2026, 7, 20, 9, 0, 0)
    dup_a = _pattern(pattern_id="a", canonical_text="same ask", keywords=["same"],
                     weight=0.9, hour_bucket=1, dow=0)
    dup_b = _pattern(pattern_id="b", canonical_text="same ask", keywords=["same"],
                     weight=0.8, hour_bucket=1, dow=0)
    extras = [
        _pattern(pattern_id=f"p{i}", canonical_text=f"ask {i}", keywords=[f"topic{i}"],
                 weight=0.9, hour_bucket=1, dow=0)
        for i in range(K + 3)
    ]
    chips = chips_for_now([dup_a, dup_b] + extras, now, recent_texts=[], k=K)
    texts = [c.text for c in chips]
    assert texts.count("same ask") == 1  # deduped by canonical text
    assert len(chips) == K               # capped at k


def test_chips_for_now_ids_are_stable_across_calls():
    now1 = datetime(2026, 7, 20, 9, 0, 0)
    now2 = datetime(2026, 7, 20, 9, 30, 0)  # same bucket/day, different minute
    p = _pattern(canonical_text="what is our roadmap", keywords=["roadmap"], weight=0.9,
                 hour_bucket=1, dow=0)
    id1 = chips_for_now([p], now1, recent_texts=[])[0].prediction_id
    id2 = chips_for_now([p], now2, recent_texts=[])[0].prediction_id
    assert id1 == id2  # stable id -> a precomputed bundle is found on a later tap


def test_chips_for_now_carries_pattern_display_text():
    now = datetime(2026, 7, 20, 9, 0, 0)
    p = _pattern(canonical_text="roadmap status", keywords=["roadmap"], weight=0.9,
                 hour_bucket=1, dow=0)
    p = Pattern(**{**p.__dict__, "display_text": "How's the roadmap looking?"})
    chips = chips_for_now([p], now, recent_texts=[])
    assert chips[0].display_text == "How's the roadmap looking?"
    assert chips[0].text == "roadmap status"  # canonical unchanged


def test_pattern_display_text_round_trips_through_file_store(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    p = Pattern(pattern_id="p1", scope="global", hour_bucket=1, dow=0, is_weekend=False,
                canonical_text="roadmap status", keywords=["roadmap"], weight=0.9,
                display_text="How's the roadmap looking?")
    store.save_patterns("global", [p])
    loaded = store.load_patterns("global")
    assert loaded[0].display_text == "How's the roadmap looking?"
    assert loaded[0].canonical_text == "roadmap status"


def test_prediction_display_text_round_trips_through_file_store(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    pred = Prediction(prediction_id="pr1", text="roadmap status", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future(),
                      display_text="How's the roadmap looking?")
    store.save_predictions("global", [pred])
    loaded = store.load_predictions("global")
    assert loaded[0].display_text == "How's the roadmap looking?"


def test_observe_exact_id_serves_bundle_regardless_of_keyword_match(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    # A prediction whose canonical text does NOT keyword-match the actual message: keyword
    # matching alone would serve nothing, but the exact tapped id must serve its bundle.
    pred = Prediction(prediction_id="pr_exact", text="what is our quarterly roadmap",
                      confidence=0.8, created_ts=1000.0, expires_ts=time_far_future(),
                      context_card_ids=["card-9"])
    store.save_predictions("global", [pred], views={"pr_exact": "precomputed exact bundle"})

    result = anticipator.observe("totally different phrasing here", "global",
                                 anticipated_id="pr_exact")

    assert result.matched is not None
    assert result.matched.prediction_id == "pr_exact"
    assert result.precomputed is not None
    assert result.precomputed.context_view == "precomputed exact bundle"
    assert result.precomputed.card_ids == ["card-9"]


def test_observe_exact_id_missing_falls_back_to_keyword_match(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None)
    pred = Prediction(prediction_id="pr1", text="what is our roadmap", confidence=0.8,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred], views={"pr1": "kw bundle"})

    # An id that is not present -> keyword matching still serves the roadmap prediction.
    result = anticipator.observe("what is our roadmap", "global", anticipated_id="nope")
    assert result.matched is not None
    assert result.matched.prediction_id == "pr1"
    assert result.precomputed.context_view == "kw bundle"


def test_apply_refresh_sets_display_text_and_never_rewrites_canonical():
    p = _pattern(canonical_text="roadmap status", keywords=["roadmap"])
    pred = Prediction(prediction_id="pr1", text="roadmap status", confidence=0.7,
                      created_ts=1.0, expires_ts=2.0)
    refinements = {"roadmap status": "How's the roadmap looking?"}
    new_patterns, new_preds = apply_refresh([p], [pred], refinements, drops=[], followups=[],
                                            now_ts=100.0)
    assert new_patterns[0].display_text == "How's the roadmap looking?"
    assert new_patterns[0].canonical_text == "roadmap status"  # canonical never rewritten
    assert new_preds[0].display_text == "How's the roadmap looking?"
    assert new_preds[0].text == "roadmap status"


def test_apply_refresh_drops_flagged_predictions():
    keep = Prediction(prediction_id="keep", text="keep this", confidence=0.7,
                      created_ts=1.0, expires_ts=2.0)
    obsolete = Prediction(prediction_id="drop", text="obsolete ask", confidence=0.7,
                          created_ts=1.0, expires_ts=2.0)
    _, new_preds = apply_refresh([], [keep, obsolete], refinements={},
                                 drops=["obsolete ask"], followups=[], now_ts=100.0)
    texts = [pr.text for pr in new_preds]
    assert texts == ["keep this"]


def test_apply_refresh_adds_followups_as_followup_source_no_pattern():
    new_patterns, new_preds = apply_refresh([], [], refinements={}, drops=[],
                                            followups=["what should I do next", "when is it due",
                                                       "a third one over the cap"],
                                            now_ts=100.0)
    assert new_patterns == []  # follow-ups create no pattern
    followups = [pr for pr in new_preds if pr.source == "followup"]
    assert len(followups) == MAX_FOLLOWUPS  # capped
    assert followups[0].display_text == followups[0].text


def test_parse_refresh_response_maps_indices_and_followups():
    candidates = ["roadmap status", "obsolete ask"]
    raw = (
        '{"candidates": ['
        '{"n": 1, "display": "How is the roadmap?", "drop": false},'
        '{"n": 2, "display": "", "drop": true}],'
        '"followups": ["what is next", "when is it due", "too many"]}'
    )
    refinements, drops, followups = parse_refresh_response(raw, candidates)
    assert refinements == {"roadmap status": "How is the roadmap?"}
    assert drops == ["obsolete ask"]
    assert followups == ["what is next", "when is it due"]  # capped at MAX_FOLLOWUPS


def test_parse_refresh_response_tolerates_fences_and_garbage():
    candidates = ["roadmap status"]
    fenced = '```json\n{"candidates": [{"n": 1, "display": "Refined", "drop": false}]}\n```'
    refinements, drops, followups = parse_refresh_response(fenced, candidates)
    assert refinements == {"roadmap status": "Refined"}
    # Garbage returns three empties, never raises.
    assert parse_refresh_response("not json at all", candidates) == ({}, [], [])


def test_anticipator_refresh_no_refiner_is_noop(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    anticipator = Anticipator(store, assembler=None, refiner=None)
    pred = Prediction(prediction_id="pr1", text="roadmap status", confidence=0.7,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred])
    anticipator.refresh("global", recent_texts=["hi"])
    # Untouched: no refiner means no model call and no change.
    loaded = store.load_predictions("global")
    assert len(loaded) == 1
    assert loaded[0].display_text == ""


def test_anticipator_refresh_applies_refiner_output_and_preserves_views(tmp_path):
    store = FilePredictionStore(str(tmp_path))
    p = _pattern(pattern_id="src", scope="global", canonical_text="roadmap status",
                 keywords=["roadmap"], weight=0.9)
    store.save_patterns("global", [p])
    pred = Prediction(prediction_id="pr1", text="roadmap status", confidence=0.7,
                      created_ts=1000.0, expires_ts=time_far_future())
    store.save_predictions("global", [pred], views={"pr1": "the bundle"})

    calls = []

    def fake_refiner(candidates, recent_texts):
        calls.append((list(candidates), list(recent_texts)))
        return ({"roadmap status": "How's the roadmap?"}, [], ["what is next"])

    anticipator = Anticipator(store, assembler=None, refiner=fake_refiner)
    anticipator.refresh("global", recent_texts=["tell me about the roadmap"])

    assert len(calls) == 1  # exactly one refiner (LLM) call
    assert calls[0][0] == ["roadmap status"]
    patterns = store.load_patterns("global")
    assert patterns[0].display_text == "How's the roadmap?"  # persisted onto the pattern
    preds = store.load_predictions("global")
    by_text = {pr.text: pr for pr in preds}
    assert by_text["roadmap status"].display_text == "How's the roadmap?"
    assert "what is next" in by_text  # follow-up stored
    assert by_text["what is next"].source == "followup"
    # The existing precomputed bundle survived the re-save.
    assert store.load_view("global", "pr1") == "the bundle"
