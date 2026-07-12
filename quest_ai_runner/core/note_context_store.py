"""Note cards for the ContextAssembler system — learned corrections from Quest AI profiles.

A NoteContextStore stores AI-rep learned corrections as searchable cards and retrieves the most
relevant ones before each task run.  It mirrors TurnContextStore in structure (same card format,
same IDF retrieval algorithm) but its cards come from Quest AI profile ``learned_notes`` rather
than from completed task turns.

Card files live under a per-rep namespace (the caller passes the namespaced dir):
    <cards_dir>/reps/<user_id>/notes/<card_id>.json

The store is stdlib-only (no heavy deps).  ``record()`` is a no-op because corrections come from
Quest, not from task outcomes.  ``assemble()`` is safe to call even if the dir is empty or absent.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.note_context_store")

# Stopword set — same as TurnContextStore so keyword extraction is consistent across the two.
_STOP = frozenset("""
a an the is are was were be been being have has had do does did will would could should may
might shall can need to of in on at by for with about as into through during before after
above below from and or but not this that these those i you he she it we they what which
who how when where why all both each few more most other some such no nor so yet either
neither s t re ve ll d m
""".split())


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _floor_relevance_gated() -> bool:
    """Whether the always-recent floor is relevance-gated (default True).

    Env ``QAR_NOTE_FLOOR_RELEVANCE_GATED``; read fresh on every call so it can be flipped
    without a restart.  Set to ``0`` / ``false`` / ``no`` / ``off`` to restore the old
    unconditional floor (the 2 most recent notes ride along on every turn regardless of
    relevance)."""
    raw = os.getenv("QAR_NOTE_FLOOR_RELEVANCE_GATED")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _card_id_for_note(note: Dict[str, Any]) -> str:
    """Derive a stable card id from a learned_note dict.

    Preference: use the Quest note's ``id`` field when present.  Fallback: sha1 of the text so
    cards whose Quest note id is missing still get a stable, collision-resistant id.
    """
    nid = note.get("id")
    if nid:
        return f"note_{nid}"
    text = str(note.get("text", "")).strip()
    return "note_" + hashlib.sha1(text.encode()).hexdigest()[:16]


class NoteContextStore:
    """ContextAssembler that stores learned corrections from Quest as cards and retrieves relevant ones.

    Mirrors TurnContextStore: cards are per-JSON-file; retrieval is IDF-weighted keyword overlap;
    the 2 most recent cards form a floor (analogous to TurnContextStore's
    always-include-last-turn guarantee) that bypasses ranking but is RELEVANCE-GATED by default:
    a floored note must share at least one meaningful keyword with the query to ride along, so a
    clearly unrelated previous topic no longer bleeds into the next turn (see
    ``_floor_relevance_gated`` for the escape hatch); up to ``max_total`` cards total are
    returned.

    Usage in the poller::

        note_store = NoteContextStore(rep_notes_dir)
        note_store.sync_from_notes(profile.get("learned_notes") or [])
        ctx = note_store.assemble(task_text)
    """

    # How many cards to return at most.
    _MAX_TOTAL = 6
    # Always include this many of the most recently created notes regardless of score.
    _ALWAYS_RECENT = 2

    def __init__(self, notes_dir: str) -> None:
        """
        Args:
            notes_dir: Path to the per-rep notes directory.  The caller namespaces it:
                ``<cards_dir>/reps/<user_id>/notes``.  The directory is created on first write.
        """
        self._dir = Path(notes_dir)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_from_notes(self, learned_notes: List[Dict[str, Any]]) -> None:
        """Write each learned note as a JSON card file; delete stale cards; never raises.

        Idempotent: an existing card with the same text is left untouched (no re-write).

        Args:
            learned_notes: The ``learned_notes`` list from a Quest AI profile.
        """
        try:
            if not learned_notes:
                # Nothing to sync; prune any cards that used to exist.
                self._prune_to({})
                return
            wanted: Dict[str, Dict[str, Any]] = {}  # card_id -> note
            for note in learned_notes:
                text = str(note.get("text", "")).strip()
                if not text:
                    continue
                cid = _card_id_for_note(note)
                wanted[cid] = note

            self._prune_to(wanted)
            self._ensure_dir()

            for cid, note in wanted.items():
                path = self._dir / f"{cid}.json"
                if path.exists():
                    # Idempotency check: if the text hasn't changed, skip the write.
                    try:
                        existing = json.loads(path.read_text())
                        if existing.get("text") == str(note.get("text", "")).strip():
                            continue
                    except Exception:
                        pass  # unreadable card — re-write it below

                text = str(note.get("text", "")).strip()
                keywords = _keywords(text)
                source = str(note.get("source", "quest"))
                card: Dict[str, Any] = {
                    "id": cid,
                    "text": text,
                    "description": f"Learned correction: {text}",
                    "keywords": keywords,
                    "created_at": str(note.get("created_at") or _now_iso()),
                    "source": source,
                }
                try:
                    path.write_text(json.dumps(card, indent=2))
                except Exception as e:
                    log.warning("note_context_store: could not write card %s: %s", cid, e)
        except Exception as e:  # noqa: BLE001 — never raises
            log.warning("note_context_store: sync_from_notes failed: %s", e)

    def _ensure_dir(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("note_context_store: could not create dir %s: %s", self._dir, e)

    def _prune_to(self, wanted: Dict[str, Any]) -> None:
        """Delete any card files whose id is not in ``wanted``."""
        if not self._dir.exists():
            return
        try:
            for p in list(self._dir.glob("*.json")):
                cid = p.stem
                if cid not in wanted:
                    try:
                        p.unlink()
                    except Exception as e:
                        log.warning("note_context_store: could not delete stale card %s: %s", p, e)
        except Exception as e:
            log.warning("note_context_store: prune failed: %s", e)

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------

    def _load_cards(self) -> List[Dict[str, Any]]:
        """Load all note cards, sorted by created_at (oldest first)."""
        if not self._dir.exists():
            return []
        cards = []
        for p in self._dir.glob("*.json"):
            try:
                cards.append(json.loads(p.read_text()))
            except Exception:
                pass
        # Sort by created_at string (ISO8601 sorts lexicographically).
        cards.sort(key=lambda c: c.get("created_at", ""))
        return cards

    def _idf_score(self, query_kw: List[str], card_kw: List[str]) -> float:
        """Simple keyword overlap count (same as TurnContextStore)."""
        if not query_kw or not card_kw:
            return 0.0
        card_set = set(card_kw)
        return sum(1 for w in query_kw if w in card_set)

    def _floor_relevant(self, query_kw: List[str], card_kw: List[str]) -> bool:
        """Minimal relevance bar for the always-recent floor.  Deliberately permissive: a single
        shared meaningful keyword (the same overlap scoring used for ranked selection) clears it,
        and when either side yields no keywords the note is KEPT (cannot judge relevance, so do
        not drop).  Only a clearly unrelated note -- both sides have keywords, zero overlap --
        is dropped."""
        if not query_kw or not card_kw:
            return True
        return self._idf_score(query_kw, card_kw) > 0.0

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Return the most relevant learned corrections as context.  Never raises.

        The 2 most recent notes form a floor that bypasses ranking, but each floored note must
        clear a minimal relevance bar against the query (``_floor_relevant``; disable via
        ``QAR_NOTE_FLOOR_RELEVANCE_GATED=0`` for the old unconditional floor).  Fills remaining
        slots with the highest-scoring (keyword-overlap) notes from the rest.  Returns up to
        ``_MAX_TOTAL`` total.  Returns an empty ``AssembledContext`` if there are no notes or
        nothing is relevant.
        """
        from .adapters import AssembledContext  # local import to avoid circular

        try:
            cards = self._load_cards()
            if not cards:
                return AssembledContext()

            query_kw = _keywords(task_text)

            # The N most recent cards bypass ranking -- but relevance-gated by default, so the
            # previous topic does not bleed into an unrelated next turn.
            recent_floor = cards[-self._ALWAYS_RECENT:]
            if _floor_relevance_gated():
                recent_floor = [
                    c for c in recent_floor
                    if self._floor_relevant(query_kw, c.get("keywords", []))
                ]
            selected_ids: set = {id(c) for c in recent_floor}

            # Score the rest by keyword overlap.
            scored = [
                (self._idf_score(query_kw, c.get("keywords", [])), i, c)
                for i, c in enumerate(cards)
                if id(c) not in selected_ids
            ]
            scored.sort(key=lambda x: (-x[0], -x[1]))  # highest score, most recent first

            selected: List[Dict[str, Any]] = list(recent_floor)
            for score, _idx, card in scored:
                if len(selected) >= self._MAX_TOTAL:
                    break
                if score > 0:
                    selected.append(card)

            # Restore chronological order so the rep sees them in the order they were learned.
            ordered = [c for c in cards if id(c) in {id(s) for s in selected}]
            if not ordered:
                # Every note (including the gated floor) was irrelevant to this query.
                return AssembledContext()

            lines = ["--- LEARNED CORRECTIONS (apply these) ---"]
            for card in ordered:
                lines.append(f"- {card.get('text', '')}")
            return AssembledContext(context_view="\n".join(lines))
        except Exception:
            from .adapters import AssembledContext
            return AssembledContext()

    # ------------------------------------------------------------------
    # Record (no-op — corrections come from Quest, not task outcomes)
    # ------------------------------------------------------------------

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """No-op.  Corrections come from Quest AI profiles, not from task outcomes.  Never raises."""
