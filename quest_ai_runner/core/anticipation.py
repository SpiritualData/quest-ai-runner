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
    ``core.text_sampling.keywords_from_text`` (stdlib tokenization, no model call).
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

SCOPE TAGS (cross-quest fence)
-------------------------------
A precomputed prediction is a discardable HINT stored under whatever scope key it was planned
under (see ``core.recent_context``'s own SCOPE TAGS section for the identical problem in the warm
recent-context store) -- including ``"global"``, which is in scope on every turn regardless of
which quest is active. Without a fence, a bundle precomputed while quest X was active and stored
under ``"global"`` is later served to a turn scoped to a completely different quest Y, since
keyword matching alone has no notion of "this prediction is ABOUT quest X". ``core.scope_tags``
closes that gap the same way it closes it for recent-context and the vector/card arms:

  * ``Anticipator.plan_next(scope_keys, ...)`` computes ``turn_tags = scope_tags_from_keys(scope_keys)``
    once per call (the quest-kind keys active this turn) and stamps it onto ``scope_tags`` for
    EVERY prediction planned this call, in every scope file it writes to -- so a "global"-scoped
    precompute slot still remembers which quest(s) were active when it was made.
  * ``Anticipator.observe(actual_text, scope_keys, ...)`` computes the requesting turn's own
    ``turn_tags`` the same way and only matches/serves/scores predictions whose ``scope_tags``
    pass ``scope_tags_allow(pred.scope_tags, turn_tags)``. A fenced prediction (tagged for a
    different quest) is invisible to this turn: not matched, not scored, not fed to the source
    pattern's EMA update.
  * A prediction with NO ``scope_tags`` (legacy data, or planned with no quest key in scope at
    all) stays visible everywhere, per the shared fence rule -- untagged is never hidden.
  * THE FOURTH LEAK (found 2026-10-07): "planned with no quest key in scope" also means the
    precompute's OWN assembler call ran with an empty ``meta["scope_tags"]``, so the per-arm fence
    inside ``VectorContextAssembler``/``FileContextStore`` was inert for that one call too (see
    their own SCOPE TAGS sections) -- an unscoped turn is free to search across EVERY quest's
    cards, same as any other unscoped read. If that search surfaces a card that IS tagged for one
    specific quest, stamping the resulting prediction with the (empty) ``turn_tags`` alone would
    cache that quest's content as an UNTAGGED bundle, served to every future turn forever
    regardless of which quest it names. ``bundle_scope_tags(assembled)`` reads back the tags the
    surfaced cards actually carry (``AssembledContext.card_metadata[i]["scope_tags"]``), and
    ``plan_next``/any chip-precompute path UNIONS it onto ``turn_tags`` before stamping the
    prediction, so the stored bundle is fenced to the quest(s) its content came from even when the
    call that built it was itself unscoped.
  * THE FIFTH LEAK (found 2026-10-07, same review): the fourth-leak union above is only safe when
    the bundle names AT MOST ONE quest. An unscoped search is free to surface cards from TWO (or
    more) different quests in the same bundle; unioning ``["quest:a", "quest:b"]`` onto a
    prediction stores it as a two-tag item, and ``scope_tags_allow`` serves a two-tag item to
    EITHER quest's turns (nonempty intersection on both sides) -- so quest A's turns would see
    quest B's content mixed into the same bundle, and vice versa. ``bundle_is_cross_quest_mixed``
    detects this (more than one distinct tag in ``bundle_scope_tags(assembled)``) and every caller
    must check it BEFORE unioning: on a mixed bundle, drop it for that prediction instead of
    storing it (no ``context_view``, no ``card_ids``) -- there is no way to retroactively split
    rendered context prose back into "the part from quest A's card" vs "quest B's card", so a
    mixed bundle is never stored as a shareable prediction, scoped or not. A bundle naming zero or
    one quest (the overwhelming common case) is completely unaffected.
  * THE SIXTH LEAK (found 2026-10-07, round 3): the fifth-leak check above looked only at
    ``bundle_scope_tags(assembled)`` ALONE, which misses a call that is ITSELF scoped: a
    precompute for a turn attached to quest A (``turn_tags=["quest:a"]``) whose bundle surfaces
    cards tagged only quest B (``bundle_scope_tags(assembled) == ["quest:b"]``, a single tag --
    not "mixed" by the bundle-only check) still produces ``["quest:a", "quest:b"]`` once unioned
    onto the prediction, which ``scope_tags_allow`` then serves to either quest.
    ``bundle_is_cross_quest_mixed`` now takes BOTH ``turn_tags`` and ``bundle_tags`` and checks
    their UNION's distinct count, since that union is exactly what would be stored.

This is opt-in and inert by construction when no quest key is ever in scope: ``scope_tags_from_keys``
returns ``[]`` for a plain ``["conv:<id>", "global"]`` turn, and ``scope_tags_allow`` treats "the
turn has no tags" as "hide nothing", so a consumer with no quest concept sees byte-for-byte the
same behavior as before this fence existed.
"""
from __future__ import annotations

# The module is split by role; this file stays the stable import path and re-exports every name.
from .anticipation_learner import (  # noqa: F401
    log,
    SIM_REINFORCE,
    MATCH_SERVE,
    CONF_FLOOR,
    ALPHA,
    PREDICTION_TTL_SECONDS,
    PRUNE_WEIGHT,
    PRUNE_AGE_DAYS,
    MAX_PATTERNS_PER_SCOPE,
    K,
    Pattern,
    Prediction,
    extract_features,
    similarity,
    score_outcome,
    update_weight,
    reinforce_or_create,
    rank_patterns,
    generate_predictions,
    chip_id,
    chips_for_now,
    match_actual,
    MAX_FOLLOWUPS,
    REFRESH_SYSTEM_PROMPT,
    build_refresh_prompt,
    parse_refresh_response,
    apply_refresh,
)
from .anticipation_store import (  # noqa: F401
    log,
    pattern_from_dict,
    prediction_from_dict,
    FilePredictionStore,
)
from .anticipation_engine import (  # noqa: F401
    log,
    assemble_with_scope_tags,
    bundle_scope_tags,
    bundle_is_cross_quest_mixed,
    Anticipator,
)
