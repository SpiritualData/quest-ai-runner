"""RecentContextStore -- scoped, item-level usage memory of recently selected context cards.

Context cards are re-selected from scratch on every turn (a hybrid keyword + vector search under a
short budget in the Orchestrator's background assembly thread; see ``core/orchestrator.py``'s
``_ctx_future`` handling). Nothing remembers which cards a conversation's earlier turns already
found relevant, so a follow-up input pays the full assembly cost again, and a timed-out assembly
leaves that turn with NO cards at all, even ones found a turn earlier. This module gives the
orchestrator a WARM, NO-LLM fallback: the cards recently selected, filtered by a cheap lexical
relevance gate so an unrelated new question never drags in stale cards.

SCOPES
------
Memory is kept in three SCOPES, consulted together:

  * ``"conv:<conv_id>"``   -- this conversation's own recent turns (narrowest, highest weight).
  * ``"quest:<quest_id>"`` -- everything recently selected anywhere on this quest.
  * ``"global"``           -- everything recently selected anywhere at all (widest, lowest weight,
    lexical relevance is ALWAYS required, never a free pass).

Each scope is its own small file (same ``<root>/recent/<sha1(key)[:16]>.json`` layout as before);
loading merges the scopes with narrower-wins-on-conflict precedence (conv > quest > global) so a
card recorded in more than one scope keeps its most specific record.

ITEM-LEVEL MEMORY
------------------
Beyond remembering WHICH cards were used, each card record now remembers WHICH of its content
items the last consolidation actually kept for a given turn, tagged with the input that turn was
answering (``input_keywords``). This lets a card surviving into a later turn re-rank its own items
so the ones previously chosen for a similar input render first -- "remember not just which cards,
but which parts of a card mattered for this kind of question."

  * ``RecentContextStore`` -- a tiny Protocol: ``record`` persists a turn's selected cards under one
    or more scope keys; ``load`` returns the merged recent records for those scopes, most-relevant
    scope first, deduped by card id. Both methods are BEST-EFFORT and NEVER raise. For convenience
    a single scope key (a bare string) is also accepted anywhere a list is documented.
  * ``FileRecentContextStore`` -- the default implementation: one JSON file per scope key under
    ``<root_dir>/recent/<sha1(key)[:16]>.json``, atomic temp-file + ``os.replace`` writes (same
    pattern as ``adapters.card_repository.FilesystemCardRepository``).
  * ``filter_relevant`` -- a PURE, NO-LLM relevance gate: normalized lexical token overlap between
    the current turn's text and each record's keywords/title, weighted by scope (conv 1.0, quest
    0.8, global 0.5) with a recency tie-break. Only the immediately previous turn's CONV-scope cards
    pass automatically on a follow-up input; quest/global records always need real overlap, so
    cross-conversation memory never drags in something unrelated.
  * ``build_item_usage_hint`` -- a PURE helper turning loaded records into a compact
    ``{card_id: [item_id, ...]}`` hint (items ranked by relevance to the current input + recency),
    threaded into a fresh assembler's ``meta`` so the consolidating LLM pass can prefer/reorder the
    items a similar past input already found useful.
  * ``render_recent_cards`` -- renders surviving records into a labeled context_view block plus
    lightweight card_metadata entries (``adapter: "recent"``), ranking each card's OWN items by
    relevance to the current input before rendering so the previously-useful ones lead.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from quest_ai_runner.adapters.tfdfidf_sampling import keywords_from_text

log = logging.getLogger("quest-ai-runner.recent_context")

# --- Scope key conventions -------------------------------------------------------------------
# A scope key is either "global", or "<scope>:<id>" for "conv"/"quest". Centralized here so
# orchestrator.py never hand-rolls the prefix strings.
GLOBAL_SCOPE_KEY = "global"


def conv_scope_key(conv_id: str) -> str:
    """The scope key for conversation ``conv_id``."""
    return f"conv:{conv_id}"


def quest_scope_key(quest_id: str) -> str:
    """The scope key for quest ``quest_id``."""
    return f"quest:{quest_id}"


def _scope_of(key: str) -> str:
    """Classify a scope key into "conv" | "quest" | "global". Unrecognized/legacy bare keys (e.g.
    a plain conversation id passed directly, as pre-scope callers did) fall back to "conv" so
    older single-key callers keep today's caps/weight/behavior unchanged."""
    if key == GLOBAL_SCOPE_KEY:
        return "global"
    if key.startswith("quest:"):
        return "quest"
    if key.startswith("conv:"):
        return "conv"
    return "conv"


def _as_key_list(scope_keys: Union[str, List[str]]) -> List[str]:
    """Accept either one scope key (a bare string) or a list, always return a deduped list."""
    if isinstance(scope_keys, str):
        scope_keys = [scope_keys] if scope_keys else []
    return list(dict.fromkeys(k for k in (scope_keys or []) if k))


# --- Caps -----------------------------------------------------------------------------------
# conv/quest scopes: keep the file small and the warm set tight so filter_relevant stays cheap.
_MAX_TURNS = 8
_MAX_CARDS = 24
_MAX_RECORD_AGE_DAYS = 14.0
# global scope: aggregates across every conversation/quest, so it gets a larger, longer-lived cap.
_MAX_TURNS_GLOBAL = 24
_MAX_CARDS_GLOBAL = 64
_MAX_RECORD_AGE_DAYS_GLOBAL = 30.0
# whole-card preview fallback, used only when a card carries no structured items.
_MAX_CARD_PREVIEW_CHARS = 500
# per-item memory caps.
_MAX_ITEMS_PER_CARD = 8
_MAX_ITEM_PREVIEW_CHARS = 300
_MAX_KEYWORDS_PER_ITEM = 24
_MAX_NOTE_LOCATOR_TEXT_CHARS = 200
# how many ranked item ids build_item_usage_hint surfaces per card.
_MAX_HINT_ITEMS_PER_CARD = 8

_RECENCY_HALF_LIFE_DAYS = 7.0
# Lexical-overlap thresholds for a record to pass the relevance gate (see filter_relevant).
_OVERLAP_RATIO_THRESHOLD = 0.15
_OVERLAP_COUNT_THRESHOLD = 2
# Scope weights: how much a scope's lexical relevance counts toward the ranking score. Conv is
# the conversation's own history (full weight); quest and global are progressively wider circles
# of memory and are damped so they never outrank a genuinely on-topic conv-scope record.
_SCOPE_WEIGHTS: Dict[str, float] = {"conv": 1.0, "quest": 0.8, "global": 0.5}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _age_days(ts: str) -> float:
    """Days since ISO timestamp ``ts``. Returns 0.0 (never old) on any parse failure."""
    try:
        parsed = datetime.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - parsed).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return 0.0


def _is_newer(ts_a: str, ts_b: str) -> bool:
    """True if ``ts_a`` is a later timestamp than ``ts_b`` (fewer days old). Never raises."""
    try:
        return _age_days(ts_a) <= _age_days(ts_b)
    except Exception:  # noqa: BLE001
        return False


@runtime_checkable
class RecentContextStore(Protocol):
    """Scoped, per-card-and-item persistence of recently selected context. NEVER raises."""

    def record(
        self, scope_keys: Union[str, List[str]], cards: List[Dict[str, Any]], user_text: str
    ) -> None:
        """Persist this turn's selected ``cards`` under EVERY key in ``scope_keys``. Best-effort."""

    def load(self, scope_keys: Union[str, List[str]]) -> List[Dict[str, Any]]:
        """Return recent card records merged across ``scope_keys``, deduped by card id with
        narrower-scope-wins precedence (conv > quest > global)."""


def _compact_locator(locator: Optional[Dict[str, Any]], itype: str) -> Dict[str, Any]:
    """A small copy of ``locator`` safe to persist: a note's raw text is truncated (everything
    else -- a file path, a collection id -- is already compact). Never raises."""
    try:
        loc = dict(locator or {})
        if itype == "note":
            text = loc.get("text")
            if isinstance(text, str) and len(text) > _MAX_NOTE_LOCATOR_TEXT_CHARS:
                loc["text"] = text[:_MAX_NOTE_LOCATOR_TEXT_CHARS].rstrip() + "…"
        return loc
    except Exception:  # noqa: BLE001
        return {}


def _build_item_records(
    raw_items: List[Any], *, ts: str, input_keywords: List[str],
    max_items: int, max_item_preview_chars: int, max_keywords: int,
) -> List[Dict[str, Any]]:
    """Turn a card's fresh ``items`` (id/type/locator/text/preview/... blocks, see
    ``adapters.card_content_render.render_card_content_blocks``) into compact, persistable item
    usage records: ``{id, type, locator, preview, last_used_ts, input_keywords}``. Never raises."""
    out: List[Dict[str, Any]] = []
    try:
        for it in (raw_items or [])[:max_items]:
            if not isinstance(it, dict):
                continue
            iid = it.get("id")
            if not iid:
                continue
            itype = it.get("type", "note")
            preview = (it.get("text") or it.get("preview") or "").strip()
            if len(preview) > max_item_preview_chars:
                preview = preview[:max_item_preview_chars].rstrip() + "…"
            out.append({
                "id": iid,
                "type": itype,
                "locator": _compact_locator(
                    it.get("locator") if isinstance(it.get("locator"), dict) else {}, itype),
                "preview": preview,
                "last_used_ts": ts,
                "input_keywords": list(input_keywords)[:max_keywords],
            })
    except Exception:  # noqa: BLE001
        log.debug("_build_item_records failed", exc_info=True)
    return out


def _union_items(
    existing: List[Dict[str, Any]], newer: List[Dict[str, Any]],
    *, max_items: int, max_keywords: int,
) -> List[Dict[str, Any]]:
    """Union two item lists by item id: the union of ``input_keywords`` (capped), the newest
    ``last_used_ts``/preview/locator win. Order follows first appearance. Never raises."""
    try:
        by_id: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for it in list(existing) + list(newer):
            if not isinstance(it, dict):
                continue
            iid = it.get("id")
            if not iid:
                continue
            if iid not in by_id:
                by_id[iid] = dict(it)
                order.append(iid)
                continue
            cur = by_id[iid]
            merged_kws = list(dict.fromkeys(
                (cur.get("input_keywords") or []) + (it.get("input_keywords") or [])
            ))[:max_keywords]
            if _is_newer(it.get("last_used_ts", ""), cur.get("last_used_ts", "")):
                cur.update(it)
            cur["input_keywords"] = merged_kws
        return [by_id[k] for k in order][:max_items]
    except Exception:  # noqa: BLE001
        log.debug("_union_items failed", exc_info=True)
        return (existing or [])[:max_items]


class FileRecentContextStore:
    """Default ``RecentContextStore``: one JSON file per SCOPE KEY under ``root_dir/recent``.

    Each file holds the last N turns (oldest first), each turn a small envelope of ``{ts,
    user_text, cards}``. ``cards`` are compact records: ``{id, title, adapter, relevance_score,
    keywords, files, preview, items, ts, turn_user_text}`` -- item-bearing cards carry their
    per-item usage records under ``items`` (capped ``max_items_per_card``); a card with no
    structured items falls back to a whole-card ``preview`` (capped ``max_preview_chars``, 500 by
    default). conv/quest scopes are capped at ``max_turns``/``max_cards`` turns/unique-card-ids and
    pruned past ``max_record_age_days``; the ``"global"`` scope uses the larger
    ``global_max_turns``/``global_max_cards``/``global_max_record_age_days`` caps since it
    aggregates across every conversation and quest. Records older than the applicable TTL are
    pruned on write.
    """

    def __init__(
        self,
        root_dir: str = ".quest-context",
        max_turns: int = _MAX_TURNS,
        max_cards: int = _MAX_CARDS,
        max_preview_chars: int = _MAX_CARD_PREVIEW_CHARS,
        max_record_age_days: float = _MAX_RECORD_AGE_DAYS,
        *,
        max_items_per_card: int = _MAX_ITEMS_PER_CARD,
        max_item_preview_chars: int = _MAX_ITEM_PREVIEW_CHARS,
        max_keywords_per_item: int = _MAX_KEYWORDS_PER_ITEM,
        global_max_turns: int = _MAX_TURNS_GLOBAL,
        global_max_cards: int = _MAX_CARDS_GLOBAL,
        global_max_record_age_days: float = _MAX_RECORD_AGE_DAYS_GLOBAL,
    ):
        self._dir = Path(root_dir) / "recent"
        self._max_turns = max_turns
        self._max_cards = max_cards
        self._max_preview_chars = max_preview_chars
        self._max_record_age_days = max_record_age_days
        self._max_items_per_card = max_items_per_card
        self._max_item_preview_chars = max_item_preview_chars
        self._max_keywords_per_item = max_keywords_per_item
        self._global_max_turns = global_max_turns
        self._global_max_cards = global_max_cards
        self._global_max_record_age_days = global_max_record_age_days

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{digest}.json"

    def _caps_for(self, key: str) -> Tuple[int, int, float]:
        """(max_turns, max_cards, max_record_age_days) for ``key``'s scope."""
        if _scope_of(key) == "global":
            return self._global_max_turns, self._global_max_cards, self._global_max_record_age_days
        return self._max_turns, self._max_cards, self._max_record_age_days

    def _load_turns(self, key: str) -> List[Dict[str, Any]]:
        path = self._path(key)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            turns = data.get("turns") if isinstance(data, dict) else None
            return turns if isinstance(turns, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _load_one_scope(self, key: str) -> List[Dict[str, Any]]:
        """Load + merge all turns for ONE scope key. Never raises; [] on any failure.

        Deduped by card id ACROSS this scope's turns: a card's SCALAR fields (title, adapter,
        keywords, files, preview, ts, ...) come from its newest occurrence, but its ``items`` are
        UNIONED across every turn it appeared in (see ``_union_items``), so item-level usage
        memory survives even when a card was re-selected several turns ago and again more
        recently. ``turn_index`` (0 = the immediately preceding turn) is stamped from the card's
        NEWEST occurrence, which is what ``filter_relevant``'s follow-up free pass keys on.
        """
        try:
            turns = self._load_turns(key)
            merged: Dict[str, Dict[str, Any]] = {}
            newest_turn_index: Dict[str, int] = {}
            for turn_index, turn in enumerate(reversed(turns)):
                for card in (turn.get("cards") or []):
                    if not isinstance(card, dict):
                        continue
                    cid = card.get("id")
                    if not cid:
                        continue
                    if cid not in newest_turn_index:
                        newest_turn_index[cid] = turn_index
                    if cid not in merged:
                        rec = dict(card)
                        rec["items"] = list(card.get("items") or [])
                        merged[cid] = rec
                    else:
                        merged[cid]["items"] = _union_items(
                            merged[cid].get("items") or [], card.get("items") or [],
                            max_items=self._max_items_per_card,
                            max_keywords=self._max_keywords_per_item,
                        )
            out: List[Dict[str, Any]] = []
            for cid, rec in merged.items():
                rec["turn_index"] = newest_turn_index.get(cid, 0)
                out.append(rec)
            return out
        except Exception:  # noqa: BLE001
            log.debug("FileRecentContextStore._load_one_scope failed for key %r", key, exc_info=True)
            return []

    def load(self, scope_keys: Union[str, List[str]]) -> List[Dict[str, Any]]:
        """Return recent card records merged across ``scope_keys``, most-relevant-scope-first,
        deduped by card id. When the SAME card id shows up under more than one scope, the
        NARROWEST scope's record wins whole (conv > quest > global) -- it is stamped with that
        scope's own ``scope`` ("conv"|"quest"|"global") and ``turn_index``. Never raises; returns
        [] on any failure or when no key resolves to anything."""
        keys = _as_key_list(scope_keys)
        if not keys:
            return []
        try:
            precedence = {"conv": 0, "quest": 1, "global": 2}
            ordered_keys = sorted(keys, key=lambda k: precedence.get(_scope_of(k), 0))
            out: List[Dict[str, Any]] = []
            seen_ids: set = set()
            for key in ordered_keys:
                scope = _scope_of(key)
                for rec in self._load_one_scope(key):
                    cid = rec.get("id")
                    if not cid or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    rec["scope"] = scope
                    out.append(rec)
            return out
        except Exception:  # noqa: BLE001
            log.debug("FileRecentContextStore.load failed for keys %r", keys, exc_info=True)
            return []

    def _build_processed_card(self, card: Dict[str, Any], *, ts: str, user_text: str) -> Optional[Dict[str, Any]]:
        """One card's persistable record for THIS turn (see class docstring for the shape). None
        when the card has no usable id."""
        cid = card.get("id")
        if not cid:
            return None
        title = card.get("title", "") or ""
        input_keywords = keywords_from_text(user_text or "")
        items = _build_item_records(
            card.get("items") or [], ts=ts, input_keywords=input_keywords,
            max_items=self._max_items_per_card,
            max_item_preview_chars=self._max_item_preview_chars,
            max_keywords=self._max_keywords_per_item,
        )
        # Whole-card preview FALLBACK, used only when the card carries no structured items (a
        # stub/keyword-only card whose value is its rendered section, not typed content items).
        preview = ""
        if not items:
            preview = (card.get("rendered_section") or card.get("text") or "").strip()
            if len(preview) > self._max_preview_chars:
                preview = preview[: self._max_preview_chars].rstrip() + "…"
        # Same stopword-filter style as core/turn_context_store.py: title + this turn's user text,
        # deduped while preserving order.
        keywords = list(dict.fromkeys(keywords_from_text(title) + input_keywords))
        return {
            "id": cid,
            "title": title,
            "adapter": card.get("adapter", ""),
            "relevance_score": card.get("relevance_score"),
            "keywords": keywords,
            "files": card.get("files") or [],
            "preview": preview,
            "items": items,
            "ts": ts,
            "turn_user_text": user_text,
        }

    def _record_one_scope(self, key: str, cards: List[Dict[str, Any]], user_text: str, *, ts: str) -> None:
        max_turns, max_cards, max_age = self._caps_for(key)
        processed: List[Dict[str, Any]] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            built = self._build_processed_card(card, ts=ts, user_text=user_text)
            if built is not None:
                processed.append(built)
        if not processed:
            return

        turns = self._load_turns(key)
        turns = [t for t in turns if _age_days(t.get("ts", "")) <= max_age]
        turns.append({"ts": ts, "user_text": user_text, "cards": processed})
        turns = turns[-max_turns:]

        # Cap total unique cards across the kept turns; walk newest-turn-first so a duplicate id
        # keeps its newest occurrence and older turns are trimmed to make room.
        keep_ids: set = set()
        for turn in reversed(turns):
            for card in turn.get("cards") or []:
                cid = card.get("id")
                if cid and len(keep_ids) < max_cards:
                    keep_ids.add(cid)
        for turn in turns:
            turn["cards"] = [c for c in (turn.get("cards") or []) if c.get("id") in keep_ids]

        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._dir), prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump({"turns": turns}, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, str(self._path(key)))
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def record(
        self, scope_keys: Union[str, List[str]], cards: List[Dict[str, Any]], user_text: str
    ) -> None:
        """Persist this turn's selected ``cards`` under EVERY key in ``scope_keys`` (the SAME turn
        record is written to each scope's file, with that scope's own caps/TTL). Best-effort,
        never raises."""
        keys = _as_key_list(scope_keys)
        if not keys or not cards:
            return
        ts = _now_iso()
        for key in keys:
            try:
                self._record_one_scope(key, cards, user_text, ts=ts)
            except Exception:  # noqa: BLE001
                log.debug("FileRecentContextStore.record failed for key %r", key, exc_info=True)


def _informative_terms(record: Dict[str, Any]) -> set:
    """Stopword-filtered terms for ``record``: its stored keywords plus fresh terms from its title."""
    return set(record.get("keywords") or []) | set(keywords_from_text(record.get("title", "") or ""))


def _recency_weight(ts: str) -> float:
    """Multiplicative recency tie-break, half-life = 7 days (same shape as TurnContextStore)."""
    try:
        return math.exp(-_age_days(ts) * math.log(2) / _RECENCY_HALF_LIFE_DAYS)
    except Exception:  # noqa: BLE001
        return 0.0


def filter_relevant(
    records: List[Dict[str, Any]],
    query_text: str,
    *,
    is_followup: bool,
    max_cards: int = 6,
) -> List[Dict[str, Any]]:
    """Pure, NO-LLM relevance gate over ``records`` (as returned by ``RecentContextStore.load``).

    A record passes when EITHER:
      * ``is_followup`` is True, the record's ``scope`` is "conv" (or unstamped -- a legacy/bare
        key defaults to conv weight), and it came from the immediately preceding turn
        (``record["turn_index"] == 0``) -- a follow-up input gets the CONVERSATION's own previous
        turn for free, no lexical check needed; or
      * its keywords/title have real lexical overlap with ``query_text``: a stopword-filtered
        token-overlap ratio >= 0.15, or >= 2 distinct informative tokens in common. Quest- and
        global-scope records ALWAYS need this real overlap -- there is no free pass outside the
        current conversation, so cross-conversation/global memory never drags in something
        unrelated to the current input.

    An unrelated new question (no overlap, and not the conv-scope's immediately previous turn) is
    dropped -- this is the core requirement: recent cards are used only when relevant to the
    CURRENT input. Passing records are ranked by ``(lexical_relevance * scope_weight) +
    recency_tie_break`` (scope weights: conv 1.0, quest 0.8, global 0.5; recency half-life 7 days)
    and capped to ``max_cards``. Never raises: returns [] on any failure. NO LLM calls anywhere in
    this function -- warm context must be free.
    """
    if not records:
        return []
    try:
        query_terms = set(keywords_from_text(query_text or ""))
        passing: List[Tuple[float, Dict[str, Any]]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            scope = record.get("scope") or "conv"
            weight = _SCOPE_WEIGHTS.get(scope, 1.0)
            forced = bool(is_followup) and scope == "conv" and record.get("turn_index") == 0
            terms = _informative_terms(record)
            overlap = query_terms & terms
            ratio = len(overlap) / len(query_terms) if query_terms else 0.0
            lexical_pass = (
                ratio >= _OVERLAP_RATIO_THRESHOLD or len(overlap) >= _OVERLAP_COUNT_THRESHOLD
            )
            if not (forced or lexical_pass):
                continue
            lexical_relevance = 1.0 if forced else ratio
            score = (lexical_relevance * weight) + _recency_weight(record.get("ts", ""))
            passing.append((score, record))
        passing.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _score, record in passing[:max_cards]]
    except Exception:  # noqa: BLE001
        log.debug("filter_relevant failed", exc_info=True)
        return []


def _rank_items(items: List[Dict[str, Any]], query_terms: set) -> List[Dict[str, Any]]:
    """Rank a card's stored item-usage records by (overlap of ``query_terms`` with the item's
    stored ``input_keywords``, descending) then recency of ``last_used_ts`` (newest first). Never
    raises: returns ``items`` in their original order on any failure."""
    try:
        def _key(it: Dict[str, Any]) -> Tuple[int, float]:
            kws = set(it.get("input_keywords") or [])
            overlap = len(query_terms & kws) if query_terms else 0
            return (overlap, _recency_weight(it.get("last_used_ts", "")))

        return sorted((it for it in items if isinstance(it, dict)), key=_key, reverse=True)
    except Exception:  # noqa: BLE001
        return list(items or [])


def build_item_usage_hint(
    records: List[Dict[str, Any]], query_text: str, *, max_items_per_card: int = _MAX_HINT_ITEMS_PER_CARD,
) -> Dict[str, List[str]]:
    """Build the compact ``{card_id: [item_id, ...]}`` hint threaded into a fresh assembler's
    ``meta["recent_item_usage"]`` (see ``adapters.hybrid_context_assembler``'s consolidation pass).

    For each record carrying stored ``items``, ranks them by relevance to ``query_text`` (overlap
    with each item's ``input_keywords``) plus recency, and lists their ids in that order. This
    reflects what past turns found useful for a similar input, REGARDLESS of whether the record
    itself passed ``filter_relevant`` this turn -- the point is to influence item ORDER within a
    card a fresh assembly re-selects on its own, not to gate which cards appear. A pure HINT: the
    consolidator is told to prefer these items, never to hard-override genuine relevance. Never
    raises; returns {} on any failure or when no record carries items.
    """
    hint: Dict[str, List[str]] = {}
    try:
        query_terms = set(keywords_from_text(query_text or ""))
        for record in records or []:
            if not isinstance(record, dict):
                continue
            cid = record.get("id")
            items = record.get("items") or []
            if not cid or not items:
                continue
            ranked = _rank_items(items, query_terms)
            ids = [it.get("id") for it in ranked if isinstance(it, dict) and it.get("id")]
            if ids:
                hint[cid] = ids[:max_items_per_card]
    except Exception:  # noqa: BLE001
        log.debug("build_item_usage_hint failed", exc_info=True)
        return {}
    return hint


def render_recent_cards(
    records: List[Dict[str, Any]], query_text: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Render surviving recent-turn ``records`` into a context_view block + card_metadata entries.

    Renders from each record's stored item previews (or its whole-card preview FALLBACK when it
    has no items), never a live re-render: the store persists a reference/preview, not the full
    card, and the card's underlying content may have changed since it was selected (a consumer
    wanting a fresh render gets one anyway on the NEXT turn's fresh assembly, which is the normal
    path). Within each card, items are ranked by relevance to ``query_text`` + recency (see
    ``_rank_items``) so the ones a similar past input found useful lead; unmatched items render
    after. Each entry's ``adapter`` is stamped ``"recent"`` so a consumer/UI can visually tell
    carried-over context apart from freshly assembled cards, using the existing card_metadata
    schema. Never raises; returns ``("", [])`` on any failure.
    """
    if not records:
        return "", []
    try:
        query_terms = set(keywords_from_text(query_text or ""))
        lines = ["--- CONTEXT FROM RECENT TURNS ---"]
        entries: List[Dict[str, Any]] = []
        for record in records:
            title = record.get("title") or record.get("id") or ""
            items = record.get("items") or []
            if items:
                ranked = _rank_items(items, query_terms)
                item_lines = [it.get("preview", "") for it in ranked if it.get("preview")]
                body = "\n".join(f"- {ln}" for ln in item_lines)
            else:
                body = record.get("preview") or ""
            section = f"### {title}\n{body}" if body else f"### {title}"
            lines.append(section)
            entries.append({
                "id": record.get("id", ""),
                "title": title,
                "relevance_score": record.get("relevance_score"),
                "file_count": len(record.get("files") or []),
                "files": record.get("files") or [],
                "adapter": "recent",
                "rendered_section": section,
                "items": items,
            })
        return "\n\n".join(lines), entries
    except Exception:  # noqa: BLE001
        log.debug("render_recent_cards failed", exc_info=True)
        return "", []
