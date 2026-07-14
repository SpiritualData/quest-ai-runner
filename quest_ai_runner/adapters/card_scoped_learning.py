"""Adapter-agnostic card-scoped learning: turn a relevance hit into a usage-tracked card reference.

Several retrieval adapters want the SAME "cross-session recall" behaviour against the turn's ACTIVE
card, and it is two moves:

  * SURFACE more widely -- widen the relevance gate to the UNION of this turn's query terms and the
    active card's own topic terms, so content on the card's topic surfaces even when it does not
    match the exact wording of this turn (``active_card_terms`` + ``gate_terms``).
  * LEARN selectively -- among the candidates that survived the caller's OWN relevance ranking, keep
    only the ones relevant to BOTH the request AND the card (the INTERSECTION test,
    ``learnable_candidates``), attach each as a reference on the active card, then re-read the card
    and stamp the landed items so they participate in the same usage-recency retrieval that files and
    collections already get (``learn_card_references``).

This module holds that logic with **no dependency on any specific adapter or content type**. The
caller supplies the reference ``ref_type``, a ``locator_fn(candidate) -> dict`` that builds the
type's locator, and the ``why`` string; nothing here hardcodes ``"conversation"`` or ``conv_id``.
Any card store duck-typed like ``FileContextStore`` (``get_card`` / ``update_card`` /
``mark_sources_used``) participates -- detected by ``callable``, never an isinstance check.

Why it lives here (not on one adapter): ``ClaudeConversationsAdapter`` was the first consumer, but a
Google-Chat adapter, a Slack adapter, a Mongo-conversation adapter, etc. all need the identical
union-gate / intersection-learn / usage-stamp behaviour. Keeping it as private methods on one
adapter would force the next adapter to copy-paste it. This is the shared seam instead.

    from .card_scoped_learning import active_card_terms, gate_terms, learnable_candidates, learn_card_references

    card_terms = active_card_terms(card_store, active_card_id)
    overlapping = [c for c, kw in cand_kw.items() if kw & gate_terms(query_terms, card_terms)]
    ...  # caller ranks `overlapping` its own way -> `selected`
    eligible = learnable_candidates(selected, lambda c: cand_kw[c], query_terms, card_terms)
    learn_card_references(
        card_store, active_card_id, eligible,
        ref_type="conversation", locator_fn=lambda cid: {"conv_id": cid},
        why="cross-session recall match", now=time.time(),
    )
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, TypeVar

from .card_content_render import content_identity_key
from .tfdfidf_sampling import keywords_from_text

T = TypeVar("T")


def active_card_terms(card_store: Optional[Any], card_id: Optional[str]) -> Set[str]:
    """Topic terms for a card -- or the EMPTY set when there is none / no store / it is unreadable.

    Pulls the card's own ``keywords`` plus the natural-language terms of its ``name`` / ``summary`` /
    ``description`` through the shared ``keywords_from_text`` tokenizer, so a widened gate compares
    like vocabularies with query terms and content digests. Returns an empty set (the neutral value
    that leaves a query-only gate unchanged) whenever there is no card id, no wired store, or the card
    cannot be read. Requires only ``get_card`` on the store; any store exposing it participates.
    Never raises.
    """
    if not card_id or card_store is None:
        return set()
    getter = getattr(card_store, "get_card", None)
    if not callable(getter):
        return set()
    try:
        card = getter(card_id)
    except Exception:  # noqa: BLE001
        return set()
    if not isinstance(card, dict):
        return set()
    terms: Set[str] = set()
    try:
        for kw in card.get("keywords", []) or []:
            token = str(kw).strip().lower()
            if len(token) > 2:
                terms.add(token)
        prose = " ".join(
            str(card.get(field) or "")
            for field in ("name", "summary", "description")
        )
        terms.update(keywords_from_text(prose))
    except Exception:  # noqa: BLE001
        return terms
    return terms


def gate_terms(query_terms: Set[str], card_terms: Set[str]) -> Set[str]:
    """The SURFACING gate: the UNION of this turn's query terms and the active card's topic terms.

    Widening to the union lets a candidate on the card's topic surface even when it does not match
    this turn's exact wording. With no active card (``card_terms`` empty) this is exactly the prior
    query-only gate, so behaviour is unchanged when card-scoped learning is inert. A one-liner, named
    so both halves of the "union for surfacing, intersection for learning" pattern live in one place.
    """
    return set(query_terms) | set(card_terms)


def learnable_candidates(
    candidates: Iterable[T],
    terms_of: Callable[[T], Set[str]],
    query_terms: Set[str],
    card_terms: Set[str],
) -> List[T]:
    """The LEARNING filter: keep only candidates relevant to BOTH the request AND the card's topic.

    A candidate is learn-eligible only when its own terms overlap ``query_terms`` (it answers THIS
    turn) AND overlap ``card_terms`` (it belongs to the card's idea) -- the INTERSECTION test, so a
    card is never diluted by something that merely matched the question but is off the card's topic.
    ``terms_of(candidate)`` yields that candidate's term set (e.g. its digest keywords). Order is
    preserved. With ``card_terms`` empty NOTHING is eligible (an inactive card learns nothing), which
    is the correct inert default. Never raises.
    """
    out: List[T] = []
    if not card_terms:
        return out
    for cand in candidates:
        try:
            terms = terms_of(cand) or set()
        except Exception:  # noqa: BLE001
            continue
        if (terms & query_terms) and (terms & card_terms):
            out.append(cand)
    return out


def _stable_item_id(ref_type: str, locator: Dict[str, Any]) -> str:
    """A deterministic, locator-stable id for a learned reference (``<ref_type>-<slug|hash>``).

    Must NOT depend on the item's position in the card (unlike ``normalize_content``'s synthesized
    id, which appends the index): the same target re-learned on a later turn has to resolve to the
    SAME id so dedupe collapses it onto the existing item instead of appending a duplicate. Builds a
    readable slug from the locator's scalar values (so ``{"conv_id": "voice-work"}`` ->
    ``conversation-voice-work``, matching the historical hand-built id), falling back to a short hash
    of the locator when no usable scalar exists. Never raises.
    """
    try:
        parts = [str(v) for _k, v in sorted(locator.items()) if isinstance(v, (str, int, float))]
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", "-".join(parts)).strip("-").lower()
        if slug:
            return f"{ref_type}-{slug[:80]}"
    except Exception:  # noqa: BLE001
        pass
    digest = hashlib.sha256(
        json.dumps(locator, sort_keys=True, default=str).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{ref_type}-{digest}"


def learn_card_references(
    card_store: Optional[Any],
    card_id: Optional[str],
    candidates: Iterable[T],
    *,
    ref_type: str,
    locator_fn: Callable[[T], Dict[str, Any]],
    why: str,
    now: float,
) -> List[str]:
    """Attach ``candidates`` as ``ref_type`` references on ``card_id`` and stamp them USED.

    For each candidate: ``locator_fn(candidate)`` builds the type's locator dict, which becomes a
    content item ``{"id", "type": ref_type, "locator", "why", "ts": now}`` added via ``update_card``.
    The store's own dedupe collapses a re-add of the same locator onto the existing item (keeping its
    id and the newest ts/why), so re-learning the same target across turns never accumulates
    duplicates. The card is then re-read and every landed item whose identity matches one we added is
    stamped via ``mark_sources_used(now=now)``, which bumps its ``last_used_ts`` / ``use_count`` so
    the reference joins the SAME usage-recency retrieval files and collections already get -- no
    re-scan of the whole source history required on later turns.

    Fully adapter-agnostic: ``ref_type`` / ``locator_fn`` / ``why`` are the caller's, and identity
    matching reuses the store's own ``content_identity_key`` so any type dedupes correctly. Requires
    only ``update_card`` (+ optionally ``get_card`` / ``mark_sources_used``) on the store, duck-typed.
    Best-effort and NEVER raises: learning is a side effect that must never discard the context the
    caller already assembled, so any failure returns what was stamped so far (possibly nothing).
    Returns the list of stamped content-item ids.
    """
    if card_store is None or not card_id:
        return []
    update = getattr(card_store, "update_card", None)
    if not callable(update):
        return []

    additions: List[Dict[str, Any]] = []
    want_keys: Set[str] = set()
    for cand in candidates:
        try:
            locator = locator_fn(cand)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(locator, dict) or not locator:
            continue
        item = {
            "id": _stable_item_id(ref_type, locator),
            "type": ref_type,
            "locator": locator,
            "why": why,
            "ts": now,
        }
        additions.append(item)
        want_keys.add(content_identity_key(item))

    if not additions:
        return []

    try:
        update(card_id, add=additions)
    except Exception:  # noqa: BLE001
        return []

    # Re-read the card so the ids we stamp are the ones that ACTUALLY landed: dedupe keeps the first
    # occurrence's id, so a re-add on a later turn resolves to the existing item's id, not ours.
    mark = getattr(card_store, "mark_sources_used", None)
    getter = getattr(card_store, "get_card", None)
    if not callable(mark):
        return []
    used_item_ids: List[str] = []
    try:
        card = getter(card_id) if callable(getter) else None
        for item in (card or {}).get("content", []) or []:
            if not isinstance(item, dict):
                continue
            if content_identity_key(item) in want_keys and item.get("id"):
                used_item_ids.append(str(item["id"]))
    except Exception:  # noqa: BLE001
        used_item_ids = [a["id"] for a in additions]
    if used_item_ids:
        try:
            mark(card_id, item_ids=used_item_ids, now=now)
        except Exception:  # noqa: BLE001
            return []
    return used_item_ids
