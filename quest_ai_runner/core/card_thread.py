"""card_thread -- per-idea threading where THE IDEA IS THE CARD.

A conversation is not one topic. People interleave ideas: they open a plan, drop into a side
question, come back with "back to the launch plan". Threading those ideas as children of a
conversation would build a second registry that duplicates what context cards already are, and
would break the moment an idea outlived its conversation (which is the normal case: cards already
cross conversations).

So there is NO thread object here. A thread IS a card, and a card's transcript spans are its
thread. Every message carries the id of the card it belongs to; "the same idea" means "the same
card id", wherever those turns were said.

WHAT THIS MODULE OWNS (all pure, no LLM calls, no storage)
----------------------------------------------------------
  * ``CardThreadContext``  -- what a consumer hands the orchestrator per turn: which card is
    ACTIVE, plus any candidate cards it always wants offered (a general/small-talk card, the cards
    recently active in this conversation). The orchestrator adds the cards its OWN retrieval
    already scored this turn, so the PRIOR costs nothing extra.
  * ``CardThreadDecision`` -- the resolved assignment for the turn: continue | switch | new.
  * ``parse_card_thread`` -- the FAIL-SAFE parser for the single field the planner emits
    (``"continue"`` | ``"switch_to:<card_id>"`` | ``"new:<label>"``). Anything unparseable,
    ambiguous, or naming an unknown card resolves to CONTINUE on the active card. A topic
    assignment must never be able to break a turn.
  * ``render_thread_hint`` -- the planner block: the active card, the candidates, and the rule.
  * ``merge_candidates`` -- the cheap PRIOR: consumer candidates + the cards this turn's retrieval
    already ranked (keyword/IDF arm + vector arm), deduped, capped. Zero extra model calls; the
    prior only NARROWS and SURFACES, the model decides.
  * ``select_thread_floor`` -- PRIORITY BLENDING for a transcript: the recent turns of THIS card,
    plus a small global floor of the very last turns whatever their card, so "as I just said"
    survives an interleave. Not hard isolation: cross-references stay possible.
  * ``rank_card_first`` -- the same blending for RECALL: this card's items first, everything else
    still reachable behind a penalty.
  * ``normalize_label`` / ``find_duplicate_label`` -- the dedupe guard, so "new:" cannot litter the
    card space with near-duplicate twins of a card that already exists.
  * lifecycle: a card OUTLIVES the work it describes. ``lifecycle_note`` renders a finished piece
    of work as knowledge, and ``CARD_LIFECYCLE_GATE`` (in ``context_doctrine``) tells the model to
    treat it as knowledge, never as open work.

Generic by construction: nothing here knows about any product, org, or storage. The consumer maps
its own entities onto card ids and lifecycle statuses.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.card_thread")

# --- the ONE field the planner emits ------------------------------------------------------------
CONTINUE = "continue"
SWITCH_PREFIX = "switch_to:"
NEW_PREFIX = "new:"

# --- resolved actions ---------------------------------------------------------------------------
ACTION_CONTINUE = "continue"
ACTION_SWITCH = "switch"
ACTION_NEW = "new"

# --- card lifecycle (generic; a consumer maps its own entity states onto these) ------------------
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_DORMANT = "dormant"
STATUS_ARCHIVED = "archived"
# Statuses whose work is FINISHED: the card stays, the work does not reopen by itself.
FINISHED_STATUSES = (STATUS_COMPLETED, STATUS_ARCHIVED)

# Hard caps so a hint can never grow unbounded into the planner prompt.
MAX_CANDIDATES = 8
MAX_LABEL_CHARS = 60

# How much of the remaining recall budget a NON-active card's material may take (the "penalty" in
# priority blending). Below 1.0 the active idea always wins the budget it needs first, and other
# ideas stay reachable with what is left, so "combine those two ideas" still works.
OTHER_CARD_BUDGET_FRACTION = 0.5

# Default floor sizes. ``card_turns`` is this card's own recent transcript; ``global_turns`` is the
# small floor of the very last messages whatever their card, so a reference to what was JUST said
# survives an interleave.
DEFAULT_CARD_FLOOR_TURNS = 8
DEFAULT_GLOBAL_FLOOR_TURNS = 2

_LABEL_CLEAN_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class CardCandidate:
    """One card the planner may assign this turn to."""
    id: str
    label: str = ""
    status: str = ""      # lifecycle, see STATUS_*; "" when the consumer does not track one
    why: str = ""         # one short line: why this card surfaced (retrieval arm, recency, ...)


@dataclass
class CardThreadContext:
    """Per-turn thread context supplied by the CONSUMER (opt-in; ``cfg.card_thread_enabled``).

    ``candidates`` are cards the consumer ALWAYS wants offered (its general/small-talk card, the
    cards recently active in this conversation, the cards of the work currently in scope). The
    orchestrator merges in the cards its own retrieval scored this turn, which is the cheap prior.

    ``allowed_types`` filters what may be pulled in from RETRIEVAL by the card's ``card_type``
    field: a consumer whose card store also holds non-topic cards (permissions, settings, derived
    doc cards) lists the types that are real ideas. Consumer-supplied ``candidates`` always pass.
    ``None`` means every retrieved card is a candidate.
    """
    active_card_id: Optional[str] = None
    active_label: str = ""
    active_status: str = ""
    candidates: List[CardCandidate] = field(default_factory=list)
    allowed_types: Optional[List[str]] = None
    max_candidates: int = MAX_CANDIDATES

    @staticmethod
    def coerce(value: Any) -> "Optional[CardThreadContext]":
        """Accept a ``CardThreadContext``, a plain dict, or None. Never raises; None on garbage."""
        if value is None:
            return None
        if isinstance(value, CardThreadContext):
            return value
        if not isinstance(value, dict):
            return None
        try:
            raw_candidates = value.get("candidates") or []
            candidates: List[CardCandidate] = []
            for c in raw_candidates:
                if isinstance(c, CardCandidate):
                    candidates.append(c)
                elif isinstance(c, dict) and c.get("id"):
                    candidates.append(CardCandidate(
                        id=str(c.get("id")), label=str(c.get("label") or ""),
                        status=str(c.get("status") or ""), why=str(c.get("why") or "")))
            allowed = value.get("allowed_types")
            return CardThreadContext(
                active_card_id=(str(value["active_card_id"])
                                if value.get("active_card_id") else None),
                active_label=str(value.get("active_label") or ""),
                active_status=str(value.get("active_status") or ""),
                candidates=candidates,
                allowed_types=[str(t) for t in allowed] if allowed else None,
                max_candidates=int(value.get("max_candidates") or MAX_CANDIDATES),
            )
        except Exception:  # noqa: BLE001 -- a malformed context must never break a turn
            log.debug("CardThreadContext.coerce failed", exc_info=True)
            return None


@dataclass
class CardThreadDecision:
    """The turn's resolved card assignment. ``action`` is always one of the ACTION_* constants."""
    action: str = ACTION_CONTINUE
    card_id: Optional[str] = None   # the card the turn belongs to (continue/switch); None for "new"
    label: Optional[str] = None     # the proposed label (new only)
    raw: str = ""                   # what the planner actually emitted, for tracing
    fell_back: bool = False         # True when a parse failure / unknown id forced the fail-safe

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "card_id": self.card_id, "label": self.label,
                "raw": self.raw, "fell_back": self.fell_back}


def normalize_label(label: str) -> str:
    """A label's dedupe key: lowercase, alphanumerics only, single-spaced. Never raises."""
    try:
        cleaned = _LABEL_CLEAN_RE.sub(" ", (label or "").strip().lower()).strip()
        return " ".join(cleaned.split())
    except Exception:  # noqa: BLE001
        return ""


def find_duplicate_label(label: str, candidates: Iterable[CardCandidate]) -> Optional[str]:
    """THE DEDUPE GUARD: the id of an existing candidate whose label is the same idea, or None.

    Cheap and pure (no embeddings): exact match on the normalized label, then containment either
    way once both sides are at least two words long ("launch plan" vs "the launch plan for v2").
    A consumer with embeddings should ALSO run its own similarity check; this guard is the floor
    that stops the obvious twins, so "new:" cannot quietly litter the card space.
    """
    key = normalize_label(label)
    if not key:
        return None
    try:
        best: Optional[str] = None
        for cand in candidates or []:
            ckey = normalize_label(cand.label)
            if not ckey:
                continue
            if ckey == key:
                return cand.id
            # Containment only counts on multi-word labels: a single shared word is a category,
            # not the same idea (the specificity discipline, applied to labels).
            if len(key.split()) >= 2 and len(ckey.split()) >= 2:
                if key in ckey or ckey in key:
                    best = best or cand.id
        return best
    except Exception:  # noqa: BLE001
        return None


def _repair_card_id(target: str, known_ids: Iterable[str]) -> Optional[str]:
    """The one known id ``target`` UNAMBIGUOUSLY refers to, or None.

    This is NOT fuzzy matching, and it is not a near-match guess (both of which are exactly what the
    fail-safe exists to refuse). It repairs ONE observed failure: a model that drops a namespace
    prefix from an id it was given verbatim ("quest_rt_ab12" for "quest:quest_rt_ab12"). A repair is
    accepted only when the emitted token equals a known id's trailing segment (after the last ":")
    and EXACTLY ONE known id matches. Two matches is an ambiguity, and an ambiguity continues the
    current card, like every other ambiguity here. Never raises.
    """
    try:
        needle = target.rsplit(":", 1)[-1].strip()
        if not needle:
            return None
        matches = [cid for cid in known_ids if cid.rsplit(":", 1)[-1] == needle]
        return matches[0] if len(matches) == 1 else None
    except Exception:  # noqa: BLE001
        return None


def parse_card_thread(
    raw: Any, *, active_card_id: Optional[str], known_ids: Optional[Iterable[str]] = None,
) -> CardThreadDecision:
    """Parse the planner's ONE card-thread field into a resolved decision. FAIL-SAFE.

    Accepted: ``"continue"``, ``"switch_to:<card_id>"``, ``"new:<label>"`` (case-insensitive on the
    keyword, never on the card id). EVERYTHING else -- an empty field, a malformed value, a
    ``switch_to:`` naming a card that is not a known candidate, a ``new:`` with no label -- resolves
    to CONTINUE on the active card, with ``fell_back=True``. This is the standing rule for the
    feature: any parse failure or ambiguity continues the current card. A misassignment must never
    cost a turn.
    """
    fallback = CardThreadDecision(action=ACTION_CONTINUE, card_id=active_card_id,
                                  raw=str(raw or ""), fell_back=True)
    try:
        if not isinstance(raw, str):
            return fallback
        value = raw.strip()
        if not value:
            return fallback
        lowered = value.lower()
        if lowered == CONTINUE:
            return CardThreadDecision(action=ACTION_CONTINUE, card_id=active_card_id, raw=value)
        if lowered.startswith(SWITCH_PREFIX):
            target = value[len(SWITCH_PREFIX):].strip()
            if not target:
                return fallback
            if known_ids is not None and target not in set(known_ids):
                repaired = _repair_card_id(target, known_ids)
                if repaired is None:
                    # The planner named a card that is not on the table, and it is not an exactly
                    # repairable form of one. Do NOT invent it and do NOT guess a near-match:
                    # continue the current card (the fail-safe).
                    log.debug("card_thread: switch_to named an unknown card %r; continuing", target)
                    return fallback
                log.debug("card_thread: repaired a mangled card id %r to %r", target, repaired)
                target = repaired
            if active_card_id and target == active_card_id:
                return CardThreadDecision(action=ACTION_CONTINUE, card_id=target, raw=value)
            return CardThreadDecision(action=ACTION_SWITCH, card_id=target, raw=value)
        if lowered.startswith(NEW_PREFIX):
            label = value[len(NEW_PREFIX):].strip().strip('"').strip("'")
            if not label:
                return fallback
            return CardThreadDecision(action=ACTION_NEW, card_id=None,
                                      label=label[:MAX_LABEL_CHARS], raw=value)
        return fallback
    except Exception:  # noqa: BLE001 -- the fail-safe is the whole point
        log.debug("parse_card_thread failed", exc_info=True)
        return fallback


def merge_candidates(
    ctx: CardThreadContext, card_metadata: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[CardCandidate]:
    """THE PRIOR, for free: consumer candidates + the cards THIS turn's retrieval already scored.

    ``card_metadata`` is the assembled context's per-card metadata (the keyword/IDF arm and the
    vector arm both populate it, already ranked). Reusing it means the prior costs zero extra model
    calls and zero extra searches: it is the same hybrid retrieval the turn ran anyway. Cards are
    filtered by ``ctx.allowed_types`` when the consumer set it AND the card carries a ``card_type``;
    consumer-supplied candidates always survive. Deduped by id (consumer entry wins, it carries the
    lifecycle status), capped at ``ctx.max_candidates``. Never raises.
    """
    out: List[CardCandidate] = []
    seen: set = set()
    try:
        active_id = ctx.active_card_id
        for cand in list(ctx.candidates or []):
            if not cand.id or cand.id in seen:
                continue
            seen.add(cand.id)
            out.append(cand)
        allowed = set(ctx.allowed_types) if ctx.allowed_types else None
        for meta in list(card_metadata or []):
            if not isinstance(meta, dict):
                continue
            cid = str(meta.get("id") or "")
            if not cid or cid in seen:
                continue
            if allowed is not None:
                ctype = str(meta.get("card_type") or "")
                if ctype not in allowed:
                    continue
            seen.add(cid)
            out.append(CardCandidate(
                id=cid,
                label=str(meta.get("title") or cid),
                status=str(meta.get("lifecycle") or ""),
                why="matched this message",
            ))
        # The ACTIVE card is always on the table, even if nothing retrieved it this turn (a follow
        # up like "make it shorter" carries none of its keywords).
        if active_id and active_id not in seen:
            out.insert(0, CardCandidate(id=active_id, label=ctx.active_label or active_id,
                                        status=ctx.active_status, why="the current topic"))
        return out[: max(1, ctx.max_candidates)]
    except Exception:  # noqa: BLE001
        log.debug("merge_candidates failed", exc_info=True)
        return out[: MAX_CANDIDATES]


def lifecycle_note(status: str, detail: str = "") -> str:
    """One line describing a card whose WORK is finished, or "" for an ongoing one.

    The card outlives the work. When the work behind it is done, the model must be told so plainly,
    in the render, so it treats the card as knowledge it may cite and build on, never as open work
    waiting to be picked up. No em dashes (this is user-facing text).
    """
    s = (status or "").strip().lower()
    if s not in FINISHED_STATUSES:
        return ""
    word = "completed" if s == STATUS_COMPLETED else "archived"
    tail = f" ({detail})" if detail else ""
    return (f"This work is {word}{tail}. The topic remains here for reference: you may discuss it, "
            f"cite it, and build on it, but do not propose working it as if it were still open "
            f"unless the user reopens it.")


def render_thread_hint(ctx: CardThreadContext, candidates: Sequence[CardCandidate]) -> str:
    """The planner block: what is active, what else is on the table, and the rule.

    A HINT, not a decision. The candidate list only narrows the space and surfaces cards a bare
    keyword read would miss; the model's judgment picks. Never raises; returns "" when there is
    nothing to say.
    """
    try:
        lines: List[str] = []
        if ctx.active_card_id:
            active_label = ctx.active_label or ctx.active_card_id
            lines.append(f"CURRENT TOPIC: [{ctx.active_card_id}] {active_label}")
        else:
            lines.append("CURRENT TOPIC: none yet (this is the first turn of the conversation).")
        if candidates:
            lines.append("TOPICS ALREADY KNOWN (candidates, ranked by how well they match this "
                         "message):")
            for cand in candidates:
                bits = [f"  - [{cand.id}] {cand.label or cand.id}"]
                extra: List[str] = []
                if cand.status and cand.status.lower() in FINISHED_STATUSES:
                    extra.append(cand.status.lower())
                if cand.why:
                    extra.append(cand.why)
                if extra:
                    bits.append(f" ({', '.join(extra)})")
                lines.append("".join(bits))
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        log.debug("render_thread_hint failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------------------------
# PRIORITY BLENDING: this card first, everything else still reachable.
# ---------------------------------------------------------------------------------------------

def message_card_id(message: Any) -> Optional[str]:
    """The card id stamped on a message dict, or None. Never raises."""
    try:
        if isinstance(message, dict):
            cid = message.get("card_id")
            return str(cid) if cid else None
    except Exception:  # noqa: BLE001
        pass
    return None


def card_id_set(card_id: Any) -> set:
    """Normalize a card SCOPE (one id, several ids, or None) into a set of ids. Never raises.

    A turn's scope is usually one card, but not always: before the planner has decided, the cheap
    PRIOR may put a second card on the table ("back to the launch plan" is about an idea that is not
    the active one, and its turns have to be reachable in the transcript that the planner reads).
    So every scoping helper here takes a SET, and a single id is just the common case.
    """
    try:
        if card_id is None:
            return set()
        if isinstance(card_id, str):
            return {card_id} if card_id else set()
        return {str(c) for c in card_id if c}
    except Exception:  # noqa: BLE001
        return set()


def belongs_to_card(message: Any, card_id: Any) -> bool:
    """True when ``message`` belongs to the card SCOPE ``card_id`` (one id or several).

    An UNSTAMPED message (no ``card_id``) always belongs: it predates threading, or was written by
    a surface that does not stamp, and dropping it would silently shrink an existing conversation's
    floor. Fail-safe means "keep it", never "hide it".
    """
    mid = message_card_id(message)
    if mid is None:
        return True
    return mid in card_id_set(card_id)


def select_thread_floor(
    messages: Sequence[Dict[str, Any]], *, card_id: Any,
    card_turns: int = DEFAULT_CARD_FLOOR_TURNS,
    global_turns: int = DEFAULT_GLOBAL_FLOOR_TURNS,
) -> List[Dict[str, Any]]:
    """The card-scoped coherence FLOOR: this card's recent turns, plus a small global floor.

    ``card_id`` is a SCOPE: one card id, or several (see ``card_id_set``).

    Two parts, merged back into their original conversation order (never re-ordered, because local
    coherence needs ORDER):

      1. the last ``card_turns`` messages that belong to ``card_id`` (an unstamped message counts,
         see ``belongs_to_card``), and
      2. the last ``global_turns`` messages whatever their card, so a reference to what was JUST
         said ("as I just told you") survives an interleave into another idea.

    This is PRIORITY BLENDING, not isolation: the sibling idea's OLDER turns do not ride along, but
    the very last exchange always does. Never raises; returns [] on any failure.
    """
    try:
        msgs = list(messages or [])
        if not msgs:
            return []
        card_slice = [m for m in msgs if belongs_to_card(m, card_id)]
        if card_turns > 0:
            card_slice = card_slice[-card_turns:]
        else:
            card_slice = []
        global_slice = msgs[-global_turns:] if global_turns > 0 else []
        keep = {id(m) for m in card_slice} | {id(m) for m in global_slice}
        return [m for m in msgs if id(m) in keep]
    except Exception:  # noqa: BLE001
        log.debug("select_thread_floor failed", exc_info=True)
        return list(messages or [])


def split_by_card(
    messages: Sequence[Dict[str, Any]], *, card_id: Any,
) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """Split ``messages`` into (in the card SCOPE, outside it). ``card_id`` may be one id or several.
    Unstamped messages count as in scope (see ``belongs_to_card``). Never raises."""
    try:
        mine: List[Dict[str, Any]] = []
        others: List[Dict[str, Any]] = []
        for m in messages or []:
            (mine if belongs_to_card(m, card_id) else others).append(m)
        return mine, others
    except Exception:  # noqa: BLE001
        return list(messages or []), []


def penalized_budget(remaining: int, fraction: float = OTHER_CARD_BUDGET_FRACTION) -> int:
    """The recall budget a NON-active card's material may use, out of what the active card left.

    This is the "penalty" of priority blending expressed as budget: the active idea takes what it
    needs first, other ideas are still reachable with a fraction of the rest (so "combine those two
    ideas" works), and nothing else can crowd out the current thread.
    """
    try:
        return max(0, int(max(0, remaining) * max(0.0, min(1.0, fraction))))
    except Exception:  # noqa: BLE001
        return 0


def rank_card_first(
    items: Sequence[Any], *, card_id: Any,
    get_card_id: Callable[[Any], Optional[str]],
    get_score: Optional[Callable[[Any], float]] = None,
    penalty: float = OTHER_CARD_BUDGET_FRACTION,
    limit: Optional[int] = None,
) -> List[Any]:
    """Rank ``items`` card-first, then globally with a PENALTY (never a hard filter).

    An item on the active card keeps its score; an item on another card keeps its score multiplied
    by ``penalty``, so a strongly matching item from another idea can still outrank a weak one from
    this idea. That is what keeps cross-references ("combine those two ideas") possible while the
    current thread still wins ties. Stable within equal scores. Never raises.
    """
    try:
        scored: List[tuple] = []
        for idx, item in enumerate(items or []):
            base = 1.0 if get_score is None else float(get_score(item) or 0.0)
            try:
                icard = get_card_id(item)
            except Exception:  # noqa: BLE001
                icard = None
            same = (icard is None) or (icard in card_id_set(card_id))
            score = base if same else base * max(0.0, min(1.0, penalty))
            scored.append((-score, idx, item))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = [t[2] for t in scored]
        return out[:limit] if limit else out
    except Exception:  # noqa: BLE001
        log.debug("rank_card_first failed", exc_info=True)
        return list(items or [])
