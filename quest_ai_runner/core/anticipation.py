"""Quest AI Anticipation Engine -- the SHARED learning core (pure) plus the runner-lane wrapper.

The goal: the assistant should feel like it already knows what the user needs. It learns
patterns of what gets asked (time of day, day of week, recent topics), predicts the likely next
ask, precomputes context for that prediction BEFORE the user asks, serves the precomputed bundle
as a cheap hint when the real ask matches, and scores EVERY prediction against the actual next
message so the system measurably improves online.

DESIGN CONTRACT (shared with any consumer that imports this module):

  * The learning algorithm lives HERE, once, as pure functions + dataclasses. A consumer with its
    own storage (e.g. an async database) reuses the pure functions with its own persistence; the
    runner lane uses ``FilePredictionStore`` + ``Anticipator`` below. No ported duplicates.
  * The pure functions do NO file or LLM I/O. Keywords come from
    ``adapters.tfdfidf_sampling.keywords_from_text`` (stdlib tokenization, no model call).
  * A prediction miss is a LEARNING SIGNAL, never user-visible breakage: the normal path is
    always the fallback, and a served bundle is only ever a cheap, discardable hint.
  * Matching is numeric scoring with thresholds. Nothing here gates control flow on keywords in
    model output (there are no model calls at all in this lane).

THE OBJECTIVE FUNCTION (``score_outcome``): a prediction's quality is the keyword similarity
between the PREDICTED ask and the ACTUAL next message, in [0, 1]. Every live prediction gets an
outcome score each turn (logged to ``prediction_log.jsonl``), so hit rate and mean score are
directly measurable, and the same score drives the online update below.

THE ONLINE UPDATE (``update_weight``): each pattern keeps an exponential moving average weight,
``w <- w + ALPHA * (score - w)``. A pattern whose predictions keep matching real asks drifts
toward 1.0; one that keeps missing decays toward 0.0 and is eventually pruned (miss decay). The
EMA is deliberately simple: bounded, online, and identical everywhere the module is reused.

PATTERN <-> PREDICTION LINKAGE: a pattern-sourced prediction's ``text`` IS its source pattern's
``canonical_text`` (see ``generate_predictions``), so an outcome is attributed to its source
pattern by canonical-text equality. Consumers applying outcome scores to stored patterns should
use the same convention (the ``Prediction`` dataclass deliberately carries no pattern id).

V2 SEMANTICS (durable patterns, read-time chips):

  * PATTERNS ARE THE DURABLE STORE. A learned pattern persists until it is pruned by miss-decay
    (its EMA weight crossing ``PRUNE_WEIGHT``), by 30-day inactivity (``PRUNE_AGE_DAYS``), or by
    the per-scope cap (``MAX_PATTERNS_PER_SCOPE``). Nothing else expires a pattern. A user who
    returns next week or next month still has every pattern they built.
  * CHIPS ARE RECOMPUTED FROM PATTERNS ON EVERY READ (``chips_for_now``). The suggestions shown are
    whichever stored patterns match the CURRENT moment's time signature (and any recent topic
    seed), ranked by ``rank_patterns``. Chips do NOT depend on stored live predictions or their
    TTL: a pattern learned days ago at this hour/weekday surfaces again now, with zero replanning.
  * STORED PREDICTIONS ARE "PRECOMPUTE SLOTS", not chip visibility. Each stored prediction pairs a
    prediction id with a precomputed context bundle. ``PREDICTION_TTL_SECONDS`` now only bounds how
    long that precomputed bundle stays trusted, never whether a chip shows. On a chip tap the
    client can pass the chip's prediction id (``anticipated_id``) so the exact slot's bundle is
    served regardless of keyword or display-text drift (see ``Anticipator.observe``).
  * DISPLAY vs CANONICAL. A prediction/pattern may carry a refined ``display_text`` (a natural
    question shown to the user); ``canonical_text``/``text`` stays the scoring + linkage key and is
    never rewritten. An optional one-LLM-per-turn refresh (see ``apply_refresh`` +
    ``Anticipator.refresh``) fills ``display_text``, drops predictions the conversation obsoleted,
    and adds a few conversational follow-up predictions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from quest_ai_runner.adapters.tfdfidf_sampling import keywords_from_text

from .adapters import AssembledContext

log = logging.getLogger("quest-ai-runner.anticipation")

# --- Frozen constants (shared vocabulary; consumers import these, never redefine them) --------
# Minimum keyword similarity between a new ask and an existing pattern for the ask to REINFORCE
# that pattern instead of creating a new one (see reinforce_or_create).
SIM_REINFORCE = 0.55
# Minimum outcome score for a live prediction to be SERVED as a match for the actual ask (see
# match_actual). Below this the prediction still gets its outcome logged, it just never serves.
MATCH_SERVE = 0.45
# Minimum confidence for a generated prediction to be kept at all (see generate_predictions).
# Also the starting weight of a brand-new pattern: fresh patterns can predict immediately, and
# one miss decays them below the floor until they recur.
CONF_FLOOR = 0.35
# Confidence at or above which a consumer may spend extra work on a prediction (e.g. generating
# a draft answer). The runner lane never generates drafts (no LLM calls here); the constant lives
# in the shared vocabulary so every consumer draws the line in the same place.
DRAFT_CONF = 0.6
# EMA step size for update_weight.
ALPHA = 0.3
# How long a stored prediction's PRECOMPUTED CONTEXT BUNDLE stays trusted (4 hours). This is a
# precompute-slot freshness bound ONLY -- it does NOT bound chip visibility. Chips are recomputed
# from the durable PATTERNS on every read (see ``chips_for_now``), so a pattern learned days ago
# still surfaces a chip now; this TTL just decides when a paired precomputed bundle is considered
# stale enough to drop rather than serve as a hint.
PREDICTION_TTL_SECONDS = 14400
# Prune a pattern once its EMA weight decays below this (it keeps missing).
PRUNE_WEIGHT = 0.05
# Prune a pattern not seen for this many days.
PRUNE_AGE_DAYS = 30
# Hard cap on stored patterns per scope (lowest weight, oldest last_seen dropped first).
MAX_PATTERNS_PER_SCOPE = 500
# How many predictions to plan per scope each turn.
K = 3

# --- Internal (non-contract) caps --------------------------------------------------------------
_MAX_PATTERN_KEYWORDS = 24


# --- Frozen dataclasses -------------------------------------------------------------------------


@dataclass
class AskFeatures:
    """The cheap, pure feature vector of one ask: WHEN it happened + WHAT it was about.

    ``hour_bucket`` is one of six day parts: 0=early (00-06), 1=morning (06-10), 2=midday
    (10-14), 3=afternoon (14-17), 4=evening (17-21), 5=night (21-24). ``dow`` is
    ``datetime.weekday()`` (0=Monday .. 6=Sunday); ``is_weekend`` is Saturday/Sunday.
    ``keywords`` are the stopword-filtered tokens of the ask text; ``scope`` is the scope key the
    features were extracted for (e.g. ``"conv:<id>"``, ``"quest:<id>"``, ``"global"``).
    """
    hour_bucket: int
    dow: int
    is_weekend: bool
    keywords: List[str]
    scope: str


@dataclass
class Pattern:
    """One learned recurring-ask pattern: a canonical ask text, the time signature it recurs at,
    its keyword profile, and an EMA ``weight`` tracking how well its predictions score against
    reality (see ``update_weight``). ``hits``/``misses`` count outcomes at/below ``MATCH_SERVE``
    for observability; the weight, not the counters, drives ranking and pruning."""
    pattern_id: str
    scope: str
    hour_bucket: int
    dow: int
    is_weekend: bool
    canonical_text: str
    keywords: List[str] = field(default_factory=list)
    weight: float = CONF_FLOOR
    hits: int = 0
    misses: int = 0
    last_seen_ts: float = 0.0
    created_ts: float = 0.0
    # Optional refined natural-question text for chips (filled by the LLM refresh, see
    # ``apply_refresh``). ``canonical_text`` stays the scoring/linkage key and is never rewritten;
    # a read-time chip prefers ``display_text`` when set, so the refinement survives across reads.
    display_text: str = ""


@dataclass
class Prediction:
    """One live prediction of the user's next ask. ``source`` is ``"pattern"`` (generated from a
    learned recurring-ask pattern) or ``"followup"`` (a consumer-side conversational follow-up
    guess; never produced in this lane). ``text`` of a pattern-sourced prediction is the source
    pattern's ``canonical_text`` (the linkage convention, see the module docstring).
    ``context_card_ids`` and ``draft_answer`` are filled by whatever precompute a consumer ran
    for this prediction (the runner lane fills card ids from its precomputed bundle and never
    fills drafts)."""
    prediction_id: str
    text: str
    confidence: float
    source: str = "pattern"
    created_ts: float = 0.0
    expires_ts: float = 0.0
    context_card_ids: List[str] = field(default_factory=list)
    draft_answer: Optional[str] = None
    # Optional refined natural-question text shown on the chip. A client renders ``display_text``
    # when set, else ``text``; because a refined display may DIVERGE from the canonical ``text``,
    # a tap should send this prediction's id (see ``Anticipator.observe(anticipated_id=...)``)
    # rather than rely on keyword matching the display text back to the prediction.
    display_text: str = ""


# --- Pure functions (no file I/O, no LLM calls, no hidden state) --------------------------------

_HOUR_BUCKET_EDGES = (6, 10, 14, 17, 21, 24)
_HOUR_BUCKET_COUNT = len(_HOUR_BUCKET_EDGES)


def extract_features(text: str, now: datetime, scope: str) -> AskFeatures:
    """Extract the ``AskFeatures`` of one ask: its day-part bucket, weekday, weekend flag, and
    stopword-filtered keywords. Pure; ``now`` is the caller's notion of the ask's local time."""
    hour = now.hour
    bucket = _HOUR_BUCKET_COUNT - 1
    for i, edge in enumerate(_HOUR_BUCKET_EDGES):
        if hour < edge:
            bucket = i
            break
    dow = now.weekday()
    return AskFeatures(
        hour_bucket=bucket,
        dow=dow,
        is_weekend=dow >= 5,
        keywords=keywords_from_text(text or ""),
        scope=scope,
    )


def similarity(kw_a: List[str], kw_b: List[str]) -> float:
    """Keyword-set similarity in [0, 1]: ``0.5 * Jaccard + 0.5 * containment of the smaller set``.

    The containment half lets a short ask fully contained in a longer one score high (the usual
    shape of "same ask, more words"); the Jaccard half keeps a tiny overlap inside a huge set from
    scoring high on containment alone. Either side empty scores 0.0 (no evidence of similarity is
    not similarity). Identical non-empty sets score 1.0.
    """
    a, b = set(kw_a or []), set(kw_b or [])
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    jaccard = inter / len(a | b)
    containment = inter / min(len(a), len(b))
    return 0.5 * jaccard + 0.5 * containment


def score_outcome(prediction_text: str, actual_text: str) -> float:
    """THE OBJECTIVE FUNCTION: how well a prediction matched the actual next ask, in [0, 1].

    Defined as the keyword ``similarity`` between the predicted text and the actual text. Every
    live prediction gets exactly this score each turn: it is what gets logged for measurable
    accuracy (hit rate = share of outcomes at/above ``MATCH_SERVE``; mean score = average of all
    outcomes), what decides serving (``match_actual``), and what feeds the EMA weight update of
    the source pattern (``update_weight``). One number, used everywhere, so online learning
    optimizes exactly what is measured. Empty prediction or actual text scores 0.0.
    """
    return similarity(keywords_from_text(prediction_text or ""),
                      keywords_from_text(actual_text or ""))


def update_weight(w: float, score: float, alpha: float = ALPHA) -> float:
    """EMA update of a pattern weight: ``w + alpha * (score - w)``.

    A stream of perfect outcomes (score 1.0) moves the weight geometrically toward 1.0; a stream
    of misses (score 0.0) decays it toward 0.0 (each step keeps ``1 - alpha`` of the old weight),
    eventually crossing ``PRUNE_WEIGHT`` and pruning the pattern. Bounded, online, and
    order-sensitive in the standard EMA way: recent outcomes matter more than old ones.
    """
    return w + alpha * (score - w)


def reinforce_or_create(
    patterns: List[Pattern], features: AskFeatures, text: str, now_ts: float,
) -> Tuple[List[Pattern], Pattern]:
    """Fold one actual ask into the pattern set for ``features.scope``; returns the updated list
    plus the pattern that absorbed the ask (reinforced or newly created). Pure: input objects are
    never mutated (updated patterns are replaced copies).

    When the best same-scope pattern's keyword ``similarity`` to the ask is at least
    ``SIM_REINFORCE``, that pattern is REINFORCED: hits + 1, ``last_seen_ts`` stamped, its weight
    EMA-updated with a full score of 1.0 (the ask recurring is the strongest evidence the pattern
    is real), its time signature moved to this latest occurrence, and its keyword profile unioned
    with the ask's (capped). Otherwise a NEW pattern is created at ``CONF_FLOOR`` weight with a
    deterministic id derived from (scope, text, now_ts).

    The returned list is then PRUNED: patterns below ``PRUNE_WEIGHT``, or unseen for more than
    ``PRUNE_AGE_DAYS``, are dropped; if more than ``MAX_PATTERNS_PER_SCOPE`` remain, the lowest
    (weight, last_seen_ts) are dropped first. The absorbed pattern itself is always kept.
    """
    best: Optional[Pattern] = None
    best_sim = 0.0
    kept: List[Pattern] = []
    for p in patterns or []:
        kept.append(p)
        if p.scope != features.scope:
            continue
        sim = similarity(features.keywords, p.keywords)
        if sim > best_sim:
            best, best_sim = p, sim

    if best is not None and best_sim >= SIM_REINFORCE:
        merged_keywords = list(dict.fromkeys(
            list(best.keywords) + list(features.keywords)))[:_MAX_PATTERN_KEYWORDS]
        absorbed = replace(
            best,
            hits=best.hits + 1,
            last_seen_ts=now_ts,
            weight=update_weight(best.weight, 1.0),
            hour_bucket=features.hour_bucket,
            dow=features.dow,
            is_weekend=features.is_weekend,
            keywords=merged_keywords,
        )
        kept = [absorbed if p is best else p for p in kept]
    else:
        pid = hashlib.sha1(
            f"{features.scope}|{text}|{now_ts}".encode("utf-8")).hexdigest()[:16]
        absorbed = Pattern(
            pattern_id=pid,
            scope=features.scope,
            hour_bucket=features.hour_bucket,
            dow=features.dow,
            is_weekend=features.is_weekend,
            canonical_text=text,
            keywords=list(features.keywords),
            weight=CONF_FLOOR,
            hits=1,
            misses=0,
            last_seen_ts=now_ts,
            created_ts=now_ts,
        )
        kept.append(absorbed)

    max_age_seconds = PRUNE_AGE_DAYS * 86400.0
    survivors = [
        p for p in kept
        if p is absorbed
        or (p.weight >= PRUNE_WEIGHT and (now_ts - p.last_seen_ts) <= max_age_seconds)
    ]
    if len(survivors) > MAX_PATTERNS_PER_SCOPE:
        others = sorted(
            (p for p in survivors if p is not absorbed),
            key=lambda p: (p.weight, p.last_seen_ts), reverse=True,
        )
        survivors = [absorbed] + others[:MAX_PATTERNS_PER_SCOPE - 1]
    return survivors, absorbed


def _time_proximity(pattern: Pattern, features: AskFeatures) -> float:
    """How close a pattern's time signature is to NOW, in (0, 1]: the mean of an hour-bucket
    component (1.0 same bucket, minus 0.25 per circular bucket step, floored at 0.25) and a
    day-of-week component (1.0 same day, 0.7 same weekend-ness, 0.4 otherwise)."""
    raw = abs(pattern.hour_bucket - features.hour_bucket)
    dist = min(raw, _HOUR_BUCKET_COUNT - raw)
    hour_score = 1.0 - 0.25 * min(dist, 3)
    if pattern.dow == features.dow:
        dow_score = 1.0
    elif pattern.is_weekend == features.is_weekend:
        dow_score = 0.7
    else:
        dow_score = 0.4
    return 0.5 * hour_score + 0.5 * dow_score


def rank_patterns(patterns: List[Pattern], features: AskFeatures) -> List[Tuple[Pattern, float]]:
    """Score every same-scope pattern for HOW LIKELY its ask is next, highest first.

    ``score = time_proximity * keyword_factor * weight``: how close the pattern's time signature
    is to now, times how much its keyword profile overlaps the current topic keywords
    (``features.keywords``), times the pattern's learned EMA weight. The keyword factor is a
    floored blend, ``0.5 + 0.5 * similarity`` (1.0 when there are no topic keywords at all), so a
    reliably time-based pattern (the every-morning ask) is damped, never zeroed, by unrelated
    recent chatter. Returns ``(pattern, score)`` pairs sorted by score descending. Pure.
    """
    scored: List[Tuple[Pattern, float]] = []
    for p in patterns or []:
        if p.scope != features.scope:
            continue
        if features.keywords:
            kw_factor = 0.5 + 0.5 * similarity(p.keywords, features.keywords)
        else:
            kw_factor = 1.0
        scored.append((p, _time_proximity(p, features) * kw_factor * p.weight))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def generate_predictions(
    patterns: List[Pattern], features: AskFeatures, recent_texts: List[str], k: int = K,
) -> List[Prediction]:
    """Generate up to ``k`` live ``Prediction``s of the next ask from the learned patterns.

    Topic keywords are ``features.keywords`` merged with the keywords of ``recent_texts`` (the
    conversation's recent messages), then patterns are ranked by ``rank_patterns`` and the top
    ones whose score clears ``CONF_FLOOR`` become predictions (confidence = the clamped score,
    source ``"pattern"``, text = the pattern's ``canonical_text`` -- the linkage convention --
    TTL ``PREDICTION_TTL_SECONDS``). Deduped by text. Pure apart from reading the clock for the
    created/expires timestamps.
    """
    merged = list(features.keywords or [])
    for t in recent_texts or []:
        merged.extend(keywords_from_text(t or ""))
    topic_features = replace(features, keywords=list(dict.fromkeys(merged)))
    now_ts = time.time()
    out: List[Prediction] = []
    seen_texts: set = set()
    for pattern, score in rank_patterns(patterns, topic_features):
        confidence = max(0.0, min(1.0, score))
        if confidence < CONF_FLOOR:
            continue
        if pattern.canonical_text in seen_texts:
            continue
        seen_texts.add(pattern.canonical_text)
        pid = hashlib.sha1(
            f"{pattern.pattern_id}|{now_ts}".encode("utf-8")).hexdigest()[:16]
        out.append(Prediction(
            prediction_id=pid,
            text=pattern.canonical_text,
            confidence=confidence,
            source="pattern",
            created_ts=now_ts,
            expires_ts=now_ts + PREDICTION_TTL_SECONDS,
            display_text=pattern.display_text or "",
        ))
        if len(out) >= k:
            break
    return out


def chip_id(scope: str, text: str) -> str:
    """The STABLE chip prediction id for ``(scope, canonical text)``: the id convention
    ``chips_for_now`` stamps on every chip it returns. Shared as a function so
    ``Anticipator.observe`` can recognize a tapped chip's id against a STORED precompute slot
    (whose own ``prediction_id`` came from ``generate_predictions`` and differs). Pure."""
    return hashlib.sha1(f"chip|{scope}|{text}".encode("utf-8")).hexdigest()[:16]


def chips_for_now(
    patterns: List[Pattern], now: datetime, recent_texts: List[str], k: int = K,
) -> List[Prediction]:
    """READ-TIME chips: rank the DURABLE patterns against the CURRENT moment and return the top
    ``k`` as ``Prediction``s, WITHOUT consulting any stored live prediction or its TTL.

    The current moment's features are extracted with an EMPTY ask text (``extract_features("",
    now, scope)``), so the ranking is driven by each pattern's time signature (hour bucket + day of
    week) and its EMA weight, softly biased by ``recent_texts`` as a topic seed (via
    ``generate_predictions``' recent-text merge). This is the whole point of v2: a pattern learned
    days ago at this hour surfaces a chip NOW, recomputed fresh, instead of depending on a
    prediction planned at the previous turn's end that has long since expired.

    ``patterns`` must all share one scope (the store is per-scope); the scope is taken from the
    first pattern. Each returned prediction gets a STABLE id derived from ``(scope, canonical_text)``
    so a precomputed bundle saved under that id is found on a later chip tap (exact-id serve),
    regardless of when the chip was regenerated. ``[]`` when there are no patterns. Pure apart from
    reading the clock inside ``generate_predictions``.
    """
    if not patterns:
        return []
    scope = patterns[0].scope
    features = extract_features("", now, scope)
    preds = generate_predictions(patterns, features, recent_texts or [], k=k)
    return [replace(p, prediction_id=chip_id(scope, p.text)) for p in preds]


def match_actual(
    live_predictions: List[Prediction], actual_text: str,
) -> Tuple[Optional[Prediction], float, List[Tuple[str, float]]]:
    """Score EVERY live prediction against the actual message (the objective function applied).

    Returns ``(best, score, outcomes)``: ``outcomes`` is one ``(prediction_id, score)`` per live
    prediction (all of them, always -- misses are learning signals too); ``score`` is the top
    outcome score (0.0 when there are no live predictions); ``best`` is the top-scoring
    prediction ONLY when its score is at least ``MATCH_SERVE``, else None (nothing is served on a
    weak match; the outcome is still recorded). Pure.
    """
    outcomes: List[Tuple[str, float]] = []
    best: Optional[Prediction] = None
    best_score = 0.0
    for p in live_predictions or []:
        s = score_outcome(p.text, actual_text)
        outcomes.append((p.prediction_id, s))
        if s > best_score:
            best, best_score = p, s
    if best is None or best_score < MATCH_SERVE:
        return None, best_score, outcomes
    return best, best_score, outcomes


# --- LLM refresh (optional, ONE call per turn max) ----------------------------------------------
#
# The refresh is the ONLY place a model is ever consulted, and it is OPTIONAL: with no refiner
# wired the engine is model-free end to end (the runner lane's default). When wired, exactly one
# batched call per turn takes the top predicted asks plus the last few conversation messages and
# returns, per candidate, a refined natural-question ``display_text`` and a ``drop`` flag (for asks
# the conversation just made obsolete), plus up to two brand-new follow-up asks. The PROMPT +
# PARSER live here so both lanes share one shape; ``apply_refresh`` is the PURE application step
# (no I/O) both lanes reuse and tests exercise directly. A consumer with a centralized prompt store
# (e.g. quest-backend) supplies its OWN prompt string and still reuses ``parse_refresh_response`` +
# ``apply_refresh``.

# Max novel follow-up predictions accepted from one refresh.
MAX_FOLLOWUPS = 2

REFRESH_SYSTEM_PROMPT = (
    "You refine a short list of predicted next questions for an assistant's suggestion chips. "
    "You never invent facts and you never rewrite the canonical meaning of a candidate. "
    "You return only valid JSON, no prose. Never use em dashes; use a comma, colon, or two "
    "sentences instead."
)


def build_refresh_prompt(candidates: List[str], recent_texts: List[str]) -> str:
    """Build the human prompt for the refresh call: the numbered candidate asks + the recent
    conversation. Generic (domain-free); a consumer with a centralized prompt store may build its
    own equivalent instead and still reuse ``parse_refresh_response``. Pure."""
    lines: List[str] = []
    lines.append("Candidates (predicted next questions), by number:")
    for i, c in enumerate(candidates or [], 1):
        lines.append(f"{i}. {c}")
    lines.append("")
    recent = [t for t in (recent_texts or []) if t]
    if recent:
        lines.append("Recent conversation (most recent last):")
        for t in recent[-6:]:
            lines.append(f"- {t}")
        lines.append("")
    lines.append(
        "Return ONLY this JSON shape:\n"
        '{\n'
        '  "candidates": [\n'
        '    {"n": 1, "display": "<a short, natural rewording of candidate 1, or empty to keep '
        'it>", "drop": false}\n'
        '  ],\n'
        f'  "followups": ["<up to {MAX_FOLLOWUPS} brand-new questions the user is likely to ask '
        'next given the conversation>"]\n'
        '}\n'
        "Rules: keep each display a short natural question with the SAME meaning as the candidate; "
        "set drop=true only when the conversation already answered or obsoleted that candidate; "
        "leave display empty when the candidate is already fine. Never use em dashes."
    )
    return "\n".join(lines)


def parse_refresh_response(
    raw: Any, candidates: List[str],
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Parse a refresh model response into ``(refinements, drops, followups)``.

    ``refinements`` maps a candidate's canonical text -> its refined ``display`` (only non-empty
    ones); ``drops`` is the canonical texts flagged obsolete; ``followups`` is up to
    ``MAX_FOLLOWUPS`` new question strings. ``candidates`` is the SAME list passed to
    ``build_refresh_prompt`` (the ``n`` in the response is 1-based into it). Accepts a dict or a raw
    JSON string (markdown fences tolerated). Never raises: anything unparseable yields three empties
    so the caller proceeds exactly as if no refresh ran. Pure."""
    refinements: Dict[str, str] = {}
    drops: List[str] = []
    followups: List[str] = []
    try:
        data = raw
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                # Strip a leading ```json / ``` fence and the trailing fence.
                text = text.split("```", 2)[1] if text.count("```") >= 2 else text
                if text.lstrip().lower().startswith("json"):
                    text = text.lstrip()[4:]
            data = json.loads(text)
        if not isinstance(data, dict):
            return refinements, drops, followups
        cands = list(candidates or [])
        for item in (data.get("candidates") or []):
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if idx < 1 or idx > len(cands):
                continue
            canonical = cands[idx - 1]
            display = str(item.get("display") or "").strip()
            if display and display != canonical:
                refinements[canonical] = display
            if bool(item.get("drop")):
                drops.append(canonical)
        for q in (data.get("followups") or []):
            q = str(q or "").strip()
            if q:
                followups.append(q)
            if len(followups) >= MAX_FOLLOWUPS:
                break
    except Exception:  # noqa: BLE001 -- a bad refresh is just no refresh
        return {}, [], []
    return refinements, drops, followups


def apply_refresh(
    patterns: List[Pattern], predictions: List[Prediction],
    refinements: Dict[str, str], drops: List[str], followups: List[str],
    now_ts: float, k: int = K,
) -> Tuple[List[Pattern], List[Prediction]]:
    """Apply a parsed refresh to ONE scope's patterns + planned predictions. Pure (no I/O).

    * ``refinements`` (canonical_text -> display_text): sets ``display_text`` on the matching
      PATTERN (so future read-time chips keep the refined wording) AND on the matching planned
      PREDICTION. ``canonical_text``/``text`` is never touched; it stays the scoring + linkage key.
    * ``drops`` (canonical_texts): removes those planned predictions for this round (the pattern is
      untouched, so it can still recur later).
    * ``followups``: appended as NEW ``source="followup"`` predictions (deduped, capped at ``k``
      total predictions, at most ``MAX_FOLLOWUPS`` new). They create NO pattern -- they are a
      conversational guess that still participates in outcome scoring via the store.

    Returns ``(updated_patterns, updated_predictions)`` (fresh lists; inputs are not mutated)."""
    refinements = refinements or {}
    drop_set = set(drops or [])

    new_patterns: List[Pattern] = []
    for p in patterns or []:
        dt = refinements.get(p.canonical_text)
        new_patterns.append(replace(p, display_text=str(dt)) if dt else p)

    new_preds: List[Prediction] = []
    for pred in predictions or []:
        if pred.text in drop_set:
            continue
        dt = refinements.get(pred.text)
        new_preds.append(replace(pred, display_text=str(dt)) if dt else pred)

    existing = {pred.text for pred in new_preds}
    added = 0
    for q in (followups or []):
        q = (q or "").strip()
        if not q or q in existing or len(new_preds) >= k or added >= MAX_FOLLOWUPS:
            continue
        existing.add(q)
        added += 1
        pid = hashlib.sha1(f"followup|{q}|{now_ts}".encode("utf-8")).hexdigest()[:16]
        new_preds.append(Prediction(
            prediction_id=pid,
            text=q,
            confidence=CONF_FLOOR,
            source="followup",
            created_ts=now_ts,
            expires_ts=now_ts + PREDICTION_TTL_SECONDS,
            display_text=q,
        ))
    return new_patterns, new_preds


# --- Runner-lane wrapper: file store + Anticipator ----------------------------------------------


def _as_key_list(scope_keys: Union[str, List[str]]) -> List[str]:
    """Accept one scope key (a bare string) or a list; return a deduped list (same convenience
    contract as ``core.recent_context``)."""
    if isinstance(scope_keys, str):
        scope_keys = [scope_keys] if scope_keys else []
    return list(dict.fromkeys(k for k in (scope_keys or []) if k))


def _pattern_from_dict(d: Dict[str, Any]) -> Optional[Pattern]:
    """Rebuild a ``Pattern`` from a stored dict, tolerating missing fields. None when unusable."""
    try:
        if not isinstance(d, dict) or not d.get("pattern_id"):
            return None
        return Pattern(
            pattern_id=str(d["pattern_id"]),
            scope=str(d.get("scope", "")),
            hour_bucket=int(d.get("hour_bucket", 0)),
            dow=int(d.get("dow", 0)),
            is_weekend=bool(d.get("is_weekend", False)),
            canonical_text=str(d.get("canonical_text", "")),
            keywords=[str(x) for x in (d.get("keywords") or [])],
            weight=float(d.get("weight", CONF_FLOOR)),
            hits=int(d.get("hits", 0)),
            misses=int(d.get("misses", 0)),
            last_seen_ts=float(d.get("last_seen_ts", 0.0)),
            created_ts=float(d.get("created_ts", 0.0)),
            display_text=str(d.get("display_text") or ""),
        )
    except Exception:  # noqa: BLE001
        return None


def _prediction_from_dict(d: Dict[str, Any]) -> Optional[Prediction]:
    """Rebuild a ``Prediction`` from a stored dict, tolerating missing fields. None when unusable."""
    try:
        if not isinstance(d, dict) or not d.get("prediction_id"):
            return None
        return Prediction(
            prediction_id=str(d["prediction_id"]),
            text=str(d.get("text", "")),
            confidence=float(d.get("confidence", 0.0)),
            source=str(d.get("source", "pattern")),
            created_ts=float(d.get("created_ts", 0.0)),
            expires_ts=float(d.get("expires_ts", 0.0)),
            context_card_ids=[str(x) for x in (d.get("context_card_ids") or [])],
            draft_answer=d.get("draft_answer"),
            display_text=str(d.get("display_text") or ""),
        )
    except Exception:  # noqa: BLE001
        return None


class FilePredictionStore:
    """The runner lane's persistence for the anticipation engine: one JSON file per SCOPE KEY
    under ``<root_dir>/predictions/<sha1(key)[:16]>.json`` (the same layout convention as
    ``core.recent_context.FileRecentContextStore``), written atomically (tempfile +
    ``os.replace``, the ``adapters.card_repository`` convention).

    Each scope file holds ``{"patterns": [...], "predictions": [...]}``; each stored prediction
    record carries an optional ``"context_view"`` sidecar (the precomputed bundle text, kept OUT
    of the shared ``Prediction`` dataclass on purpose: it is a runner-lane persistence detail).
    Outcome records append to ``<root_dir>/predictions/prediction_log.jsonl`` (one JSON object
    per line) so accuracy (hit rate, mean score) is measurable offline. Every method is
    best-effort and never raises.
    """

    def __init__(self, root_dir: str = ".quest-context"):
        self._dir = Path(root_dir) / "predictions"

    @property
    def log_path(self) -> Path:
        """Where outcome records append (JSONL, one object per line)."""
        return self._dir / "prediction_log.jsonl"

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{digest}.json"

    def _load_state(self, key: str) -> Dict[str, Any]:
        path = self._path(key)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, key: str, state: Dict[str, Any]) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._dir), prefix=".tmp_", suffix=".json")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp_path, str(self._path(key)))
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            log.debug("FilePredictionStore write failed for key %r", key, exc_info=True)

    # --- patterns -------------------------------------------------------------------------------

    def load_patterns(self, scope_key: str) -> List[Pattern]:
        """The stored patterns for ``scope_key``. [] on any failure."""
        try:
            raw = self._load_state(scope_key).get("patterns") or []
            return [p for p in (_pattern_from_dict(d) for d in raw) if p is not None]
        except Exception:  # noqa: BLE001
            return []

    def save_patterns(self, scope_key: str, patterns: List[Pattern]) -> None:
        """Persist ``patterns`` for ``scope_key``, preserving the scope's live predictions."""
        try:
            state = self._load_state(scope_key)
            state["patterns"] = [asdict(p) for p in (patterns or [])]
            self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("save_patterns failed for key %r", scope_key, exc_info=True)

    # --- live predictions -----------------------------------------------------------------------

    def load_predictions(self, scope_key: str) -> List[Prediction]:
        """The LIVE (unexpired) predictions for ``scope_key``. Expired ones are silently
        dropped on load. [] on any failure."""
        try:
            now_ts = time.time()
            raw = self._load_state(scope_key).get("predictions") or []
            out: List[Prediction] = []
            for d in raw:
                p = _prediction_from_dict(d)
                if p is not None and p.expires_ts > now_ts:
                    out.append(p)
            return out
        except Exception:  # noqa: BLE001
            return []

    def save_predictions(
        self, scope_key: str, predictions: List[Prediction],
        views: Optional[Dict[str, str]] = None,
    ) -> None:
        """Persist the scope's live predictions (REPLACING the previous set), preserving its
        patterns. ``views`` maps prediction_id to its precomputed context_view text (the bundle),
        stored as a sidecar field on each prediction record."""
        try:
            state = self._load_state(scope_key)
            records: List[Dict[str, Any]] = []
            for p in predictions or []:
                rec = asdict(p)
                view = (views or {}).get(p.prediction_id, "")
                if view:
                    rec["context_view"] = view
                records.append(rec)
            state["predictions"] = records
            self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("save_predictions failed for key %r", scope_key, exc_info=True)

    def load_view(self, scope_key: str, prediction_id: str) -> str:
        """The precomputed context_view stored for ``prediction_id`` under ``scope_key``
        ("" when none was precomputed or on any failure)."""
        try:
            for d in self._load_state(scope_key).get("predictions") or []:
                if isinstance(d, dict) and d.get("prediction_id") == prediction_id:
                    return str(d.get("context_view") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def clear_predictions(self, scope_key: str) -> None:
        """Drop the scope's live predictions (they were judged this turn), keeping patterns."""
        try:
            state = self._load_state(scope_key)
            if state.get("predictions"):
                state["predictions"] = []
                self._write_state(scope_key, state)
        except Exception:  # noqa: BLE001
            log.debug("clear_predictions failed for key %r", scope_key, exc_info=True)

    # --- outcome log ------------------------------------------------------------------------------

    def append_log(self, record: Dict[str, Any]) -> None:
        """Append one outcome record to ``prediction_log.jsonl`` (best-effort append-only)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.debug("append_log failed", exc_info=True)


@dataclass
class MatchResult:
    """What ``Anticipator.observe`` found for the actual message: the served prediction (None
    when nothing cleared ``MATCH_SERVE``), its precomputed context bundle (None when the match
    carried no precomputed view), and the top outcome score."""
    matched: Optional[Prediction] = None
    precomputed: Optional[AssembledContext] = None
    score: float = 0.0


class Anticipator:
    """The runner lane's anticipation engine: observe -> learn -> plan_next, per turn.

    ``store`` is a ``FilePredictionStore``; ``assembler`` is an optional ``ContextAssembler``
    whose ``assemble(predicted_text)`` precomputes a context bundle per planned prediction.
    Pattern-based only: NO LLM calls anywhere in this class (precompute uses whatever the wired
    assembler does, which for the runner lane's file/keyword arms is model-free; an assembler
    that spends model tokens is the consumer's own choice). Every method is best-effort and
    never raises. ``close()`` asks a running ``plan_next`` to stop between bundles, so a consumer
    can register the background thread like an index thread and join it at shutdown.
    """

    def __init__(self, store: FilePredictionStore, assembler: Any = None,
                 refiner: Any = None):
        self.store = store
        self.assembler = assembler
        # Optional ONE-CALL-PER-TURN refresh hook. A callable
        # ``refiner(candidates: List[str], recent_texts: List[str]) ->
        # (refinements: Dict[str, str], drops: List[str], followups: List[str])`` -- the ONLY
        # place this engine ever spends a model token. None (the default) keeps the lane model-free;
        # ``refresh`` is then a no-op. Each lane builds its own refiner (its own prompt + provider)
        # and both reuse ``parse_refresh_response`` + ``apply_refresh``.
        self.refiner = refiner
        self._closed = False

    def close(self) -> None:
        """Ask any in-flight ``plan_next`` to stop at its next checkpoint. Idempotent."""
        self._closed = True

    # --- turn start -------------------------------------------------------------------------------

    def observe(
        self, actual_text: str, scope_keys: Union[str, List[str]],
        now: Optional[datetime] = None, anticipated_id: Optional[str] = None,
    ) -> MatchResult:
        """Score the ACTUAL message against every live prediction in every scope, log every
        outcome (hits AND misses -- the objective function's measurable record), apply the miss
        decay / hit reinforcement to the source patterns (EMA via ``update_weight``, attributed
        by the canonical-text linkage), consume the judged predictions, and return the best match
        across scopes with its precomputed bundle when one cleared ``MATCH_SERVE``.

        ``anticipated_id`` (optional) is the EXACT prediction id the client tapped: when given and a
        stored prediction with that id exists in any consulted scope, that slot's precomputed bundle
        is served DIRECTLY, taking precedence over keyword matching (a refined ``display_text`` can
        diverge from the canonical ``text`` enough that keyword matching would miss). The id may be
        either a STORED prediction's own id (a ``plan_next`` slot) or the STABLE chip id
        ``chips_for_now`` stamps (``chip_id(scope, text)``); both resolve to the same slot. The
        outcome is still scored and logged for learning either way. Keyword matching remains the
        fallback for a typed ask with no id.

        Scope keys are consulted in the order given (pass narrowest first: conv, quest, global);
        on a score tie the earlier key wins. Never raises: any failure returns an empty
        ``MatchResult`` and the turn proceeds on the normal path.
        """
        result = MatchResult()
        served_exact = False
        try:
            now_ts = (now or datetime.now()).timestamp()
            for key in _as_key_list(scope_keys):
                preds = self.store.load_predictions(key)
                if not preds:
                    continue
                best, best_score, outcomes = match_actual(preds, actual_text)
                # EXACT-ID SERVE: honor the id the client tapped, regardless of keyword score.
                # Match a stored slot by its own id OR by its stable chip id, since a chip
                # returned by ``chips_for_now`` carries ``chip_id(scope, text)``, not the id
                # ``plan_next`` stored the precompute slot under.
                if anticipated_id and not served_exact:
                    exact = next(
                        (p for p in preds
                         if p.prediction_id == anticipated_id
                         or chip_id(key, p.text) == anticipated_id), None)
                    if exact is not None:
                        view = self.store.load_view(key, exact.prediction_id)
                        result.matched = exact
                        result.score = score_outcome(exact.text, actual_text)
                        result.precomputed = (
                            AssembledContext(context_view=view,
                                             card_ids=list(exact.context_card_ids))
                            if view else None
                        )
                        served_exact = True
                if not served_exact and best is not None and best_score > result.score:
                    view = self.store.load_view(key, best.prediction_id)
                    result.matched = best
                    result.score = best_score
                    result.precomputed = (
                        AssembledContext(context_view=view,
                                         card_ids=list(best.context_card_ids))
                        if view else None
                    )
                self._apply_outcomes(key, preds, outcomes)
                for pid, s in outcomes:
                    self.store.append_log({
                        "ts": now_ts,
                        "event": "outcome",
                        "scope_key": key,
                        "prediction_id": pid,
                        "score": round(s, 4),
                        "matched": s >= MATCH_SERVE,
                    })
                self.store.clear_predictions(key)
        except Exception:  # noqa: BLE001 -- anticipation must never break a turn
            log.debug("Anticipator.observe failed", exc_info=True)
        return result

    # --- read time ------------------------------------------------------------------------------

    def chips_for_now(
        self, scope_keys: Union[str, List[str]], recent_texts: Optional[List[str]] = None,
        now: Optional[datetime] = None, k: int = K,
    ) -> Dict[str, List[Prediction]]:
        """READ-TIME chips per scope: load each scope's DURABLE patterns from the store and rank
        them against the CURRENT moment via the module-level ``chips_for_now`` (time signature +
        EMA weight, softly topic-seeded by ``recent_texts``). This is the consumer entry point for
        "what should the suggestion chips show right now": it depends only on the patterns, never
        on stored live predictions or their TTL, so a pattern learned days ago at this hour still
        surfaces. Returns ``{scope_key: [Prediction, ...]}`` with only the scopes that produced
        chips; each chip carries the stable ``chip_id(scope, text)`` a later tap can send back as
        ``observe(anticipated_id=...)``. Never raises ({} on any failure)."""
        out: Dict[str, List[Prediction]] = {}
        try:
            now_dt = now or datetime.now()
            for key in _as_key_list(scope_keys):
                patterns = self.store.load_patterns(key)
                if not patterns:
                    continue
                chips = chips_for_now(patterns, now_dt, list(recent_texts or []), k=k)
                if chips:
                    out[key] = chips
        except Exception:  # noqa: BLE001 -- a read-time chip failure is just no chips
            log.debug("Anticipator.chips_for_now failed", exc_info=True)
        return out

    def _apply_outcomes(
        self, scope_key: str, preds: List[Prediction], outcomes: List[Tuple[str, float]],
    ) -> None:
        """EMA-update the source patterns of the judged predictions (canonical-text linkage):
        each outcome score moves its pattern's weight via ``update_weight`` and bumps
        hits/misses. Best-effort; never raises."""
        try:
            if not outcomes:
                return
            patterns = self.store.load_patterns(scope_key)
            if not patterns:
                return
            text_by_pid = {p.prediction_id: p.text for p in preds}
            updated: List[Pattern] = []
            changed = False
            for pattern in patterns:
                for pid, s in outcomes:
                    if text_by_pid.get(pid) != pattern.canonical_text:
                        continue
                    pattern = replace(
                        pattern,
                        weight=update_weight(pattern.weight, s),
                        hits=pattern.hits + (1 if s >= MATCH_SERVE else 0),
                        misses=pattern.misses + (0 if s >= MATCH_SERVE else 1),
                    )
                    changed = True
                updated.append(pattern)
            if changed:
                self.store.save_patterns(scope_key, updated)
        except Exception:  # noqa: BLE001
            log.debug("Anticipator outcome application failed", exc_info=True)

    # --- turn end ---------------------------------------------------------------------------------

    def learn(
        self, actual_text: str, scope_keys: Union[str, List[str]],
        now: Optional[datetime] = None,
    ) -> None:
        """Fold the turn's ACTUAL ask into each scope's pattern set (``reinforce_or_create``:
        a recurring ask reinforces its pattern, a novel one creates a new pattern; prune + cap
        applied). Never raises."""
        try:
            now_dt = now or datetime.now()
            now_ts = now_dt.timestamp()
            for key in _as_key_list(scope_keys):
                features = extract_features(actual_text, now_dt, key)
                patterns, _ = reinforce_or_create(
                    self.store.load_patterns(key), features, actual_text, now_ts)
                self.store.save_patterns(key, patterns)
        except Exception:  # noqa: BLE001
            log.debug("Anticipator.learn failed", exc_info=True)

    def plan_next(
        self, scope_keys: Union[str, List[str]], recent_texts: List[str],
        now: Optional[datetime] = None,
    ) -> List[Prediction]:
        """Generate the next turn's live predictions per scope (``generate_predictions``) and,
        when an assembler is wired, PRECOMPUTE each prediction's context bundle
        (``assembler.assemble(predicted_text)``: the bundle's context_view is stored as the
        prediction's sidecar view, its card ids on ``context_card_ids``). Replaces each scope's
        previous live set. Returns everything planned (all scopes). Checks ``close()`` between
        bundles so a joining shutdown never waits on a long precompute chain. Never raises."""
        planned: List[Prediction] = []
        try:
            now_dt = now or datetime.now()
            seed_text = " ".join((recent_texts or [])[-3:])
            for key in _as_key_list(scope_keys):
                if self._closed:
                    break
                patterns = self.store.load_patterns(key)
                if not patterns:
                    continue
                features = extract_features(seed_text, now_dt, key)
                preds = generate_predictions(patterns, features, recent_texts or [], k=K)
                if not preds:
                    self.store.clear_predictions(key)
                    continue
                views: Dict[str, str] = {}
                if self.assembler is not None:
                    for p in preds:
                        if self._closed:
                            break
                        try:
                            assembled = self.assembler.assemble(p.text)
                            view = (getattr(assembled, "context_view", "") or "").strip()
                            if view:
                                views[p.prediction_id] = view
                            p.context_card_ids = [
                                str(c) for c in (getattr(assembled, "card_ids", None) or [])]
                        except Exception:  # noqa: BLE001 -- a failed precompute is just no bundle
                            log.debug("prediction precompute failed", exc_info=True)
                self.store.save_predictions(key, preds, views)
                planned.extend(preds)
        except Exception:  # noqa: BLE001
            log.debug("Anticipator.plan_next failed", exc_info=True)
        return planned

    def refresh(
        self, scope_keys: Union[str, List[str]], recent_texts: List[str],
        now: Optional[datetime] = None,
    ) -> None:
        """OPTIONAL one-LLM-call-per-turn refresh of the planned predictions across scopes.

        No-op (zero model calls) when no ``refiner`` is wired -- the model-free default. When wired,
        gathers the currently planned predictions across ``scope_keys``, makes EXACTLY ONE
        ``refiner`` call for the whole turn, then applies the result per scope via ``apply_refresh``:
        a refined ``display_text`` is stamped onto the matching PATTERN (so later read-time chips
        keep it) and prediction, conversation-obsoleted predictions are dropped for this round, and
        up to ``MAX_FOLLOWUPS`` novel follow-up predictions are stored (narrowest scope only; they
        create no pattern). Existing precomputed bundles are preserved. Call AFTER ``plan_next`` in
        the same turn-end background thread, so it stays off the response path and single-flight.
        Never raises: any refiner failure leaves the planned predictions exactly as ``plan_next``
        left them."""
        if self.refiner is None:
            return
        try:
            now_ts = (now or datetime.now()).timestamp()
            keys = _as_key_list(scope_keys)
            scope_state: Dict[str, Tuple[List[Pattern], List[Prediction]]] = {}
            candidates: List[str] = []
            seen: set = set()
            for key in keys:
                patterns = self.store.load_patterns(key)
                preds = self.store.load_predictions(key)
                scope_state[key] = (patterns, preds)
                for p in preds:
                    if p.text not in seen:
                        seen.add(p.text)
                        candidates.append(p.text)
            if not candidates:
                return
            refinements, drops, followups = self.refiner(
                candidates, list(recent_texts or []))
            if not (refinements or drops or followups):
                return
            for i, key in enumerate(keys):
                if self._closed:
                    break
                patterns, preds = scope_state[key]
                # Follow-ups are conversational, so they belong only to the narrowest scope.
                scope_followups = followups if i == 0 else []
                new_patterns, new_preds = apply_refresh(
                    patterns, preds, refinements, drops, scope_followups, now_ts)
                # Preserve each surviving prediction's precomputed bundle across the re-save.
                views: Dict[str, str] = {}
                for p in new_preds:
                    v = self.store.load_view(key, p.prediction_id)
                    if v:
                        views[p.prediction_id] = v
                self.store.save_patterns(key, new_patterns)
                self.store.save_predictions(key, new_preds, views)
        except Exception:  # noqa: BLE001 -- a refresh failure must never break a turn
            log.debug("Anticipator.refresh failed", exc_info=True)
