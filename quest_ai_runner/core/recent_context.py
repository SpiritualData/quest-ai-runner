"""RecentContextStore -- per-conversation persistence of recently selected context cards.

Context cards are re-selected from scratch on every turn (a hybrid keyword + vector search under a
short budget in the Orchestrator's background assembly thread; see ``core/orchestrator.py``'s
``_ctx_future`` handling). Nothing remembers which cards a conversation's earlier turns already
found relevant, so a follow-up input pays the full assembly cost again, and a timed-out assembly
leaves that turn with NO cards at all, even ones found a turn earlier. This module gives the
orchestrator a WARM, NO-LLM fallback: the cards a conversation recently selected, filtered by a
cheap lexical relevance gate so an unrelated new question never drags in stale cards.

  * ``RecentContextStore`` -- a tiny Protocol: ``record`` persists a turn's selected cards for a
    conversation key; ``load`` returns the recent records for that key, most-recent-first, deduped
    by card id. Both methods are BEST-EFFORT and NEVER raise.
  * ``FileRecentContextStore`` -- the default implementation: one JSON file per conversation under
    ``<root_dir>/recent/<sha1(key)[:16]>.json``, atomic temp-file + ``os.replace`` writes (same
    pattern as ``adapters.card_repository.FilesystemCardRepository``).
  * ``filter_relevant`` -- a PURE, NO-LLM relevance gate: normalized lexical token overlap between
    the current turn's text and each record's keywords/title, with a recency tie-break. The
    immediately previous turn's cards pass automatically on a follow-up input; older ones still
    need real overlap. This is the whole point of the module: warm context must be FREE.
  * ``render_recent_cards`` -- renders surviving records into a labeled context_view block plus
    lightweight card_metadata entries (``adapter: "recent"``) so a UI can tell them apart from
    freshly assembled cards.
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
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from quest_ai_runner.adapters.tfdfidf_sampling import keywords_from_text

log = logging.getLogger("quest-ai-runner.recent_context")

# Caps: keep the file small and the warm set tight so filter_relevant stays cheap to run every turn.
_MAX_TURNS = 8
_MAX_CARDS = 24
_MAX_PREVIEW_CHARS = 1500
_MAX_RECORD_AGE_DAYS = 14.0
_RECENCY_HALF_LIFE_DAYS = 7.0
# Lexical-overlap thresholds for a record to pass the relevance gate (see filter_relevant).
_OVERLAP_RATIO_THRESHOLD = 0.15
_OVERLAP_COUNT_THRESHOLD = 2


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


@runtime_checkable
class RecentContextStore(Protocol):
    """Per-conversation persistence of the cards recently selected for it. NEVER raises."""

    def record(self, key: str, cards: List[Dict[str, Any]], user_text: str) -> None:
        """Persist this turn's selected ``cards`` for conversation ``key``. Best-effort."""

    def load(self, key: str) -> List[Dict[str, Any]]:
        """Return recent card records for ``key``, most-recent-first, deduped by card id (newest kept)."""


class FileRecentContextStore:
    """Default ``RecentContextStore``: one JSON file per conversation under ``root_dir/recent``.

    Each file holds the last ``max_turns`` turns (oldest first), each turn a small envelope of
    ``{ts, user_text, cards}``. ``cards`` are compact records: ``{id, title, adapter,
    relevance_score, keywords, files, preview, ts, turn_user_text}`` -- a PREVIEW, not the full
    rendered card, capped at ``max_preview_chars`` (references + compact preview; re-resolve fresh
    when possible, which is exactly what the next turn's fresh assembly does). Records older than
    ``max_record_age_days`` are pruned on write, and the file is capped at ``max_turns`` turns /
    ``max_cards`` unique card ids (newest wins on a duplicate id).
    """

    def __init__(
        self,
        root_dir: str = ".quest-context",
        max_turns: int = _MAX_TURNS,
        max_cards: int = _MAX_CARDS,
        max_preview_chars: int = _MAX_PREVIEW_CHARS,
        max_record_age_days: float = _MAX_RECORD_AGE_DAYS,
    ):
        self._dir = Path(root_dir) / "recent"
        self._max_turns = max_turns
        self._max_cards = max_cards
        self._max_preview_chars = max_preview_chars
        self._max_record_age_days = max_record_age_days

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{digest}.json"

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

    def load(self, key: str) -> List[Dict[str, Any]]:
        """Return recent card records for ``key``, most-recent-first, deduped by card id.

        Never raises; returns [] on any failure or missing key/file. Stamps ``turn_index`` (0 =
        the immediately preceding turn, 1 = the one before that, ...) onto each returned record so
        ``filter_relevant`` can auto-pass the last turn's cards on a follow-up input.
        """
        if not key:
            return []
        try:
            turns = self._load_turns(key)
            out: List[Dict[str, Any]] = []
            seen_ids: set = set()
            for turn_index, turn in enumerate(reversed(turns)):
                for card in (turn.get("cards") or []):
                    cid = card.get("id")
                    if not cid or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    rec = dict(card)
                    rec["turn_index"] = turn_index
                    out.append(rec)
            return out
        except Exception:  # noqa: BLE001
            log.debug("FileRecentContextStore.load failed for key %r", key, exc_info=True)
            return []

    def record(self, key: str, cards: List[Dict[str, Any]], user_text: str) -> None:
        """Persist this turn's selected ``cards`` for conversation ``key``. Best-effort, never raises."""
        if not key or not cards:
            return
        try:
            ts = _now_iso()
            processed: List[Dict[str, Any]] = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                cid = card.get("id")
                if not cid:
                    continue
                title = card.get("title", "") or ""
                preview = (card.get("rendered_section") or card.get("text") or "").strip()
                if len(preview) > self._max_preview_chars:
                    preview = preview[: self._max_preview_chars].rstrip() + "…"
                # Same stopword-filter style as core/turn_context_store.py: title + this turn's
                # user text, deduped while preserving order.
                keywords = list(dict.fromkeys(
                    keywords_from_text(title) + keywords_from_text(user_text or "")
                ))
                processed.append({
                    "id": cid,
                    "title": title,
                    "adapter": card.get("adapter", ""),
                    "relevance_score": card.get("relevance_score"),
                    "keywords": keywords,
                    "files": card.get("files") or [],
                    "preview": preview,
                    "ts": ts,
                    "turn_user_text": user_text,
                })
            if not processed:
                return

            turns = self._load_turns(key)
            turns = [t for t in turns if _age_days(t.get("ts", "")) <= self._max_record_age_days]
            turns.append({"ts": ts, "user_text": user_text, "cards": processed})
            turns = turns[-self._max_turns:]

            # Cap total unique cards across the kept turns; walk newest-turn-first so a duplicate
            # id keeps its newest occurrence and older turns are trimmed to make room.
            keep_ids: set = set()
            for turn in reversed(turns):
                for card in turn.get("cards") or []:
                    cid = card.get("id")
                    if cid and len(keep_ids) < self._max_cards:
                        keep_ids.add(cid)
            for turn in turns:
                turn["cards"] = [c for c in (turn.get("cards") or []) if c.get("id") in keep_ids]

            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._dir), prefix=".tmp_", suffix=".json"
            )
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
      * ``is_followup`` is True and it came from the immediately preceding turn
        (``record["turn_index"] == 0``) -- a follow-up input gets the previous turn's cards for
        free, no lexical check needed; or
      * its keywords/title have real lexical overlap with ``query_text``: a stopword-filtered
        token-overlap ratio >= 0.15, or >= 2 distinct informative tokens in common.

    An unrelated new question (no overlap, and not the immediately previous turn) is dropped --
    this is the core requirement: recent cards are used only when relevant to the CURRENT input.
    Passing records are ranked by (forced-pass or overlap ratio) plus a recency tie-break (7-day
    half-life) and capped to ``max_cards``. Never raises: returns [] on any failure. NO LLM calls
    anywhere in this function -- warm context must be free.
    """
    if not records:
        return []
    try:
        query_terms = set(keywords_from_text(query_text or ""))
        passing: List[Tuple[float, Dict[str, Any]]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            forced = bool(is_followup) and record.get("turn_index") == 0
            terms = _informative_terms(record)
            overlap = query_terms & terms
            ratio = len(overlap) / len(query_terms) if query_terms else 0.0
            lexical_pass = (
                ratio >= _OVERLAP_RATIO_THRESHOLD or len(overlap) >= _OVERLAP_COUNT_THRESHOLD
            )
            if not (forced or lexical_pass):
                continue
            score = (1.0 if forced else ratio) + _recency_weight(record.get("ts", ""))
            passing.append((score, record))
        passing.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _score, record in passing[:max_cards]]
    except Exception:  # noqa: BLE001
        log.debug("filter_relevant failed", exc_info=True)
        return []


def render_recent_cards(records: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Render surviving recent-turn ``records`` into a context_view block + card_metadata entries.

    Renders from each record's stored PREVIEW, never a live re-render: the store persists a
    reference/preview, not the full card, and the card's underlying content may have changed since
    it was selected (a consumer wanting a fresh render gets one anyway on the NEXT turn's fresh
    assembly, which is the normal path). Each entry's ``adapter`` is stamped ``"recent"`` so a
    consumer/UI can visually tell carried-over context apart from freshly assembled cards, using
    the existing card_metadata schema. Never raises; returns ``("", [])`` on any failure.
    """
    if not records:
        return "", []
    try:
        lines = ["--- CONTEXT FROM RECENT TURNS (this conversation) ---"]
        entries: List[Dict[str, Any]] = []
        for record in records:
            title = record.get("title") or record.get("id") or ""
            preview = record.get("preview") or ""
            section = f"### {title}\n{preview}" if preview else f"### {title}"
            lines.append(section)
            entries.append({
                "id": record.get("id", ""),
                "title": title,
                "relevance_score": record.get("relevance_score"),
                "file_count": len(record.get("files") or []),
                "files": record.get("files") or [],
                "adapter": "recent",
                "rendered_section": section,
            })
        return "\n\n".join(lines), entries
    except Exception:  # noqa: BLE001
        log.debug("render_recent_cards failed", exc_info=True)
        return "", []
