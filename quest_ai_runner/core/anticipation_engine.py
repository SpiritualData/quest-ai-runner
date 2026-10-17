"""Anticipation engine, the per-turn driver: ``Anticipator`` (observe -> learn -> plan_next ->
refresh) with ``MatchResult`` and the scope-tag helpers it uses to fence a precomputed bundle to
the quest(s) it came from (``assemble_with_scope_tags``, ``bundle_scope_tags``,
``bundle_is_cross_quest_mixed``).

``core.anticipation`` holds the design contract (read its SCOPE TAGS section) and re-exports every
name here; the learning functions are in ``anticipation_learner`` and the file store is
``anticipation_store``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from .adapters import AssembledContext
from .anticipation_learner import (
    K,
    MATCH_SERVE,
    Pattern,
    Prediction,
    apply_refresh,
    chip_id,
    extract_features,
    generate_predictions,
    match_actual,
    reinforce_or_create,
    score_outcome,
    update_weight,
)
from .anticipation_store import FilePredictionStore
from .scope_tags import scope_tags_allow, scope_tags_from_keys, union_scope_tags

log = logging.getLogger("quest-ai-runner.anticipation")


def as_key_list(scope_keys: Union[str, List[str]]) -> List[str]:
    """Accept one scope key (a bare string) or a list; return a deduped list (same convenience
    contract as ``core.recent_context``)."""
    if isinstance(scope_keys, str):
        scope_keys = [scope_keys] if scope_keys else []
    return list(dict.fromkeys(k for k in (scope_keys or []) if k))



def assemble_with_scope_tags(assembler: Any, text: str, scope_tags: List[str]) -> Any:
    """Precompute one bundle via ``assembler.assemble``, threading ``scope_tags`` through
    ``meta`` (see the module docstring's SCOPE TAGS section) when there are any to thread.

    Calls ``assembler.assemble(text, meta={"scope_tags": scope_tags})`` when ``scope_tags`` is
    non-empty; falls back to the plain ``assembler.assemble(text)`` call when ``scope_tags`` is
    empty OR the assembler's ``assemble`` does not accept a ``meta`` keyword at all (a ``TypeError``
    on the call, not on anything the assembler does internally). This is the ONE place both
    ``Anticipator.plan_next`` (below) and any consumer precomputing bundles OUTSIDE the Anticipator
    (e.g. a consumer's own chip-precompute path) get this assembler-compatibility fallback, so it
    is written once (hard rule #4: a second consumer's need belongs in the library). Callers are
    expected to wrap this in their own try/except for a genuinely failed precompute; this helper
    itself does not swallow anything other than the meta-keyword TypeError.
    """
    if scope_tags:
        try:
            return assembler.assemble(text, meta={"scope_tags": list(scope_tags)})
        except TypeError:
            return assembler.assemble(text)
    return assembler.assemble(text)


def bundle_scope_tags(assembled: Any) -> List[str]:
    """The scope tags actually carried by the cards/hits inside a precomputed ``assembled``
    bundle (``AssembledContext.card_metadata``'s optional ``"scope_tags"`` field -- see
    ``VectorContextAssembler``/``FileContextStore``), unioned into one deduped list.

    THE FOURTH-LEAK FIX (see the module docstring's SCOPE TAGS section): ``assemble_with_scope_tags``
    only fences OUT cards that disagree with a non-empty ``scope_tags`` -- when the call it wraps
    ran with EMPTY scope_tags (an unscoped turn: no quest attached, or genuinely ambiguous), the
    fence inside the assembler is inert by the documented rule "the turn has no tags -> nothing is
    hidden", so the search is free to surface a hit that IS tagged for one specific quest. A
    precomputed bundle built from that unfiltered search, stored and stamped with the call's own
    (empty) ``scope_tags``, is then an UNTAGGED prediction -- visible to every later turn, scoped or
    not, including one scoped to a DIFFERENT quest than the quest whose card actually fed the text.

    Callers (``Anticipator.plan_next`` below, and any consumer precomputing bundles OUTSIDE it, e.g.
    a chip-precompute path) must UNION this onto the call's own ``scope_tags`` before stamping a
    stored prediction, so a bundle that incidentally pulled in quest-X content gets fenced from
    quest-Y turns even when the precompute itself ran unscoped. Returns ``[]`` for a bundle with no
    tagged cards (the common case; stamping stays exactly the call's own scope_tags). Never raises.
    """
    try:
        metas = getattr(assembled, "card_metadata", None) or []
        return union_scope_tags(*(m.get("scope_tags") for m in metas if isinstance(m, dict)))
    except Exception:  # noqa: BLE001 -- a malformed bundle must never block the precompute
        return []


def bundle_is_cross_quest_mixed(turn_tags: List[str], bundle_tags: List[str]) -> bool:
    """True when ``turn_tags`` (this call's own scope) UNIONED with ``bundle_tags`` (as returned
    by ``bundle_scope_tags``) names MORE THAN ONE distinct quest -- i.e. storing their union as a
    prediction's ``scope_tags`` would fence the bundle to more than one quest.

    THE FIFTH LEAK (found 2026-10-07, same review that caught the fourth): unioning
    ``bundle_scope_tags(assembled)`` onto a prediction's ``scope_tags`` (the fourth-leak fix
    above) is only safe when the RESULT names at most ONE quest. Checking ``bundle_tags`` ALONE
    (the first version of this function) missed the SIXTH leak (found 2026-10-07, round 3): a
    precompute call SCOPED to quest A (``turn_tags=["quest:a"]``) whose bundle surfaces cards
    tagged ONLY quest B (``bundle_tags=["quest:b"]``, a single tag -- not "mixed" by the old,
    bundle-only check) still produces ``union_scope_tags(turn_tags, bundle_tags) ==
    ["quest:a", "quest:b"]`` once unioned, which ``scope_tags_allow`` then serves to EITHER
    quest's turns. The union-of-both-inputs check here catches that case the same way it already
    caught the unscoped multi-quest-bundle case (``turn_tags=[]``, ``bundle_tags=["quest:a",
    "quest:b"]``): either way, the UNION that would actually be stored is what must stay at most
    one quest, not either input alone.

    Callers must check this BEFORE unioning and storing a precomputed bundle: on True, drop the
    bundle for this prediction (no ``context_view``, no ``card_ids``) rather than store a leaking
    one -- there is no way to retroactively split already-rendered context prose back into "the
    part that came from quest A's card" vs "quest B's card", so the two choices the review named
    were (a) drop the offending cards before the bundle is ever rendered, or (b) drop the whole
    bundle. This module takes (b): it is far simpler, it cannot mis-split rendered text, and it
    costs nothing in the overwhelmingly common case (a bundle naming zero or one quest, matching
    the turn's own scope, which includes every genuinely single-quest conversation) -- a mixed
    bundle just falls back to a fresh, correctly-scoped assembly at serve time, exactly the
    existing "a served bundle is a discardable hint, the fresh assembly still leads" contract
    every consumer already honors. Never raises.
    """
    try:
        return len(set(turn_tags or []) | set(bundle_tags or [])) > 1
    except Exception:  # noqa: BLE001
        return False


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

        SCOPE TAGS (see the module docstring): this turn's own ``turn_tags`` (the quest-kind keys
        among ``scope_keys``) is computed once, and each scope's loaded predictions are split into
        ALLOWED (``scope_tags_allow(p.scope_tags, turn_tags)``) and FENCED. Only allowed
        predictions may match, be served (keyword match OR exact ``anticipated_id``), or be
        scored/logged as an outcome and folded into their source pattern's EMA -- a fenced
        prediction was planned for a DIFFERENT quest than this turn's, so this turn must not learn
        from it or be answered by it. Fenced predictions are left otherwise untouched (they still
        belong to their own quest's later turn); the scope's live set is still cleared at the end
        of the key's block exactly as before, since the turn happened either way and ``plan_next``
        replaces the set wholesale at turn end regardless.
        """
        result = MatchResult()
        served_exact = False
        try:
            now_ts = (now or datetime.now()).timestamp()
            keys = as_key_list(scope_keys)
            turn_tags = scope_tags_from_keys(keys)
            for key in keys:
                preds = self.store.load_predictions(key)
                if not preds:
                    continue
                allowed = [p for p in preds if scope_tags_allow(p.scope_tags, turn_tags)]
                if allowed:
                    best, best_score, outcomes = match_actual(allowed, actual_text)
                    # EXACT-ID SERVE: honor the id the client tapped, regardless of keyword score.
                    # Match a stored slot by its own id OR by its stable chip id, since a chip
                    # returned by ``chips_for_now`` carries ``chip_id(scope, text)``, not the id
                    # ``plan_next`` stored the precompute slot under. Only searched among ALLOWED
                    # predictions: a tapped id resolving to a fenced (different-quest) slot must
                    # not be served.
                    if anticipated_id and not served_exact:
                        exact = next(
                            (p for p in allowed
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
                    self._apply_outcomes(key, allowed, outcomes)
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
        ``observe(anticipated_id=...)``. Never raises ({} on any failure).

        SCOPE TAGS (see the module docstring): the same ``scope_tags_allow`` fence used by
        ``observe`` is applied here too, over each chip's own ``scope_tags`` against this call's
        ``turn_tags``, for consistency -- a chip is untagged by default (patterns carry no
        scope_tags of their own, so a freshly generated chip inherits none), which is always
        visible per the shared fence rule, so this is a no-op today and simply keeps this method
        wired the same way should a future chip ever carry one.
        """
        out: Dict[str, List[Prediction]] = {}
        try:
            now_dt = now or datetime.now()
            keys = as_key_list(scope_keys)
            turn_tags = scope_tags_from_keys(keys)
            for key in keys:
                patterns = self.store.load_patterns(key)
                if not patterns:
                    continue
                chips = chips_for_now(patterns, now_dt, list(recent_texts or []), k=k)
                chips = [c for c in chips if scope_tags_allow(c.scope_tags, turn_tags)]
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
            for key in as_key_list(scope_keys):
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
        bundles so a joining shutdown never waits on a long precompute chain. Never raises.

        SCOPE TAGS (see the module docstring): ``turn_tags`` (the quest-kind keys among
        ``scope_keys``) is computed ONCE for the whole call and stamped onto ``scope_tags`` for
        EVERY prediction planned this call, in every scope it is saved to -- including a
        ``"global"``-scoped one, which is exactly how a bundle precomputed while one quest is
        active stays fenced out of a later turn scoped to a different quest (see
        ``Anticipator.observe``). The precompute call threads ``turn_tags`` through the assembler's
        ``meta`` via ``assemble_with_scope_tags`` (falls back to the old positional-only call for
        an assembler whose ``assemble`` does not accept ``meta`` at all). Once a bundle comes back,
        its final ``scope_tags`` is ``turn_tags`` UNIONED with ``bundle_scope_tags(assembled)`` --
        the tags actually carried by the cards the bundle surfaced -- so a call that ran UNSCOPED
        (``turn_tags`` empty: no quest attached, or genuinely ambiguous) but still pulled in a
        quest-tagged card does not store that bundle untagged (visible to every quest forever);
        it is fenced to the quest(s) the content actually came from instead. EXCEPT when
        ``bundle_is_cross_quest_mixed(turn_tags, bundle_scope_tags(assembled))`` is True -- the
        UNION of this call's own scope and the bundle's cards would name more than one distinct
        quest (either because the bundle itself mixes two quests, or because a turn scoped to
        one quest surfaced a bundle tagged only with a DIFFERENT one) -- in which case the union
        is never computed: the bundle is dropped for that prediction (no ``context_view``, no
        ``card_ids``, ``scope_tags`` stays exactly ``turn_tags``) rather than stored as a tag set
        that would let EITHER quest's turns see the whole mixed bundle. See
        ``bundle_is_cross_quest_mixed``.
        """
        planned: List[Prediction] = []
        try:
            now_dt = now or datetime.now()
            seed_text = " ".join((recent_texts or [])[-3:])
            keys = as_key_list(scope_keys)
            turn_tags = scope_tags_from_keys(keys)
            for key in keys:
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
                for p in preds:
                    p.scope_tags = list(turn_tags)
                views: Dict[str, str] = {}
                if self.assembler is not None:
                    for p in preds:
                        if self._closed:
                            break
                        try:
                            assembled = assemble_with_scope_tags(self.assembler, p.text, turn_tags)
                            leaked_tags = bundle_scope_tags(assembled)
                            if bundle_is_cross_quest_mixed(turn_tags, leaked_tags):
                                # Fifth/sixth-leak fix: UNIONING turn_tags with the bundle's own
                                # surfaced-card tags would name MORE THAN ONE distinct quest (see
                                # ``bundle_is_cross_quest_mixed`` -- either because the bundle
                                # itself mixes two quests, or because a turn scoped to quest A
                                # surfaced a bundle tagged only quest B). Storing that union
                                # would let ``scope_tags_allow`` serve the bundle to EITHER
                                # quest's turns. Drop the bundle for this prediction instead of
                                # storing a leaking one; the next real turn just falls back to a
                                # fresh, correctly-scoped assembly.
                                p.context_card_ids = []
                                p.scope_tags = list(turn_tags)
                                continue
                            view = (getattr(assembled, "context_view", "") or "").strip()
                            if view:
                                views[p.prediction_id] = view
                            p.context_card_ids = [
                                str(c) for c in (getattr(assembled, "card_ids", None) or [])]
                            # Fourth-leak fix: UNION the quest tags the bundle's own cards carry
                            # onto this prediction, so an unscoped precompute (turn_tags empty)
                            # that still surfaced a quest-tagged card fences that bundle out of a
                            # later DIFFERENT quest's turn instead of being stored untagged /
                            # visible everywhere. See ``bundle_scope_tags``. Safe here because the
                            # mixed case (more than one distinct tag) was already ruled out above.
                            p.scope_tags = union_scope_tags(turn_tags, leaked_tags)
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
        left them.

        SCOPE TAGS (see the module docstring): the same fence ``observe`` applies is applied here
        to which predictions are gathered as refiner candidates and which get refreshed. Each
        scope's loaded predictions are split into ALLOWED (``scope_tags_allow(p.scope_tags,
        turn_tags)``) and FENCED; only allowed predictions become refiner candidates or get
        refined/dropped/have follow-ups added via ``apply_refresh``. Fenced predictions (planned
        for a different quest than this call's) are carried through UNCHANGED into the re-save, so
        this refresh never drops or rewrites a slot that belongs to another quest's turn."""
        if self.refiner is None:
            return
        try:
            now_ts = (now or datetime.now()).timestamp()
            keys = as_key_list(scope_keys)
            turn_tags = scope_tags_from_keys(keys)
            scope_state: Dict[str, Tuple[List[Pattern], List[Prediction], List[Prediction]]] = {}
            candidates: List[str] = []
            seen: set = set()
            for key in keys:
                patterns = self.store.load_patterns(key)
                preds = self.store.load_predictions(key)
                allowed = [p for p in preds if scope_tags_allow(p.scope_tags, turn_tags)]
                fenced = [p for p in preds if not scope_tags_allow(p.scope_tags, turn_tags)]
                scope_state[key] = (patterns, allowed, fenced)
                for p in allowed:
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
                patterns, allowed, fenced = scope_state[key]
                # Follow-ups are conversational, so they belong only to the narrowest scope.
                scope_followups = followups if i == 0 else []
                new_patterns, new_allowed = apply_refresh(
                    patterns, allowed, refinements, drops, scope_followups, now_ts)
                new_preds = new_allowed + fenced
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
