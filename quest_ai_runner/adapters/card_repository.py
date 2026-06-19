"""CardRepository — the pluggable PERSISTENCE boundary for context cards.

A context card (see ``file_context_store``) is a persistent, source-agnostic topic card. The
``FileContextStore`` owns all of the card LOGIC (selection / IDF / recency / the card-update API /
``export_for_embedding`` / bootstrap), but the raw PERSISTENCE of a card (where it is stored, how it
is read and written) is a separate concern. This module defines that boundary so the same logic can
persist cards to per-card JSON files (the default) OR to a database, without duplicating the logic.

  * ``CardRepository`` — a tiny Protocol: the complete set of persistence operations the store
    performs on cards (load-all / read / write / delete / exists / revision). Every method is
    BEST-EFFORT and NEVER raises; on any failure a reader returns ``None``/``{}``/``False`` and a
    writer returns ``False``, exactly mirroring the filesystem behavior so a non-filesystem repo is
    a drop-in.

  * ``FilesystemCardRepository`` — the default implementation: one ``<cards_dir>/<id>.json`` file
    per card, atomic temp-file + ``os.replace`` writes, and a cheap ``(max_child_mtime, file_count)``
    change-stamp used to invalidate the store's in-memory cache when another process writes a card.

Generic by construction: no org, collection, or path specifics live here. A consumer that wants to
persist cards in its own store (e.g. a database) implements ``CardRepository`` and injects it via
``FileContextStore(..., card_repository=...)``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

# Name of the bootstrap meta sidecar written next to cards on the filesystem. The filesystem repo
# ignores it when enumerating cards (it is store meta-state, not a card). Kept in sync with
# ``file_context_store._BOOTSTRAP_META_FILE``.
_BOOTSTRAP_META_FILE = "bootstrap_meta.json"


@runtime_checkable
class CardRepository(Protocol):
    """Persistence boundary for context cards. Every method is BEST-EFFORT and NEVER raises.

    A card is a plain JSON-serializable ``dict`` keyed by its string ``id``. The store layers all of
    its in-memory caching and card logic ON TOP of this interface, so an implementation only has to
    persist and fetch raw card dicts. The error contract matches the filesystem behavior exactly:
    readers degrade to an empty/``None`` result, writers and ``delete`` return ``False`` on failure.
    """

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Return every stored card as ``{card_id: card_dict}``. Returns ``{}`` on any error."""
        ...

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Return the card stored under ``card_id``, or ``None`` if absent/unreadable."""
        ...

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Upsert ``card`` under ``card_id`` atomically. Returns ``True`` on a successful write."""
        ...

    def delete(self, card_id: str) -> bool:
        """Delete the card stored under ``card_id``. Returns ``True`` when it is gone afterwards."""
        ...

    def exists(self, card_id: str) -> bool:
        """Return ``True`` when a card is stored under ``card_id``."""
        ...

    def revision(self) -> Any:
        """A cheap change-stamp that differs whenever the underlying store changed.

        The store compares this between reads to know when its in-memory cache is stale (another
        process wrote a card). It only needs to be cheap and to CHANGE on any external write; its
        concrete type is opaque to the store. Returns a stable sentinel on any error.
        """
        ...

    # ------------------------------------------------------------------
    # OPTIONAL capability: native text search.
    # ------------------------------------------------------------------
    # A repository MAY additionally expose ``search_cards`` when its backing store can do native
    # full-text / keyword search (e.g. a Qdrant-backed repo). It is intentionally NOT part of the
    # required surface above: ``FileContextStore`` detects it by duck-typing (``hasattr``), never an
    # isinstance check, so a repo that omits it is fully valid and the store transparently falls back
    # to scanning ``load_all()`` with its own in-app IDF. When present, the store uses the returned
    # cards as the CANDIDATE POOL for the keyword arm and then applies its existing IDF ranking /
    # confidence gate / recency over those candidates. The default ``FilesystemCardRepository`` does
    # NOT implement it (so its behavior is byte-for-byte unchanged).
    #
    #   def search_cards(
    #       self, query: str, *, limit: int
    #   ) -> Optional[Dict[str, Dict[str, Any]]]:
    #       """Return the cards most relevant to ``query`` by NATIVE text search as
    #       ``{card_id: card_dict}``, or ``None`` when this repo has no native text search.
    #       Best-effort: return ``None`` (fall back to in-app IDF) on any error, never raise.
    #       """


class FilesystemCardRepository:
    """Default ``CardRepository``: one ``<cards_dir>/<id>.json`` file per card.

    This is exactly the filesystem persistence ``FileContextStore`` used inline before the
    persistence boundary was extracted: atomic temp-file + ``os.replace`` writes, JSON parsing on
    read, a glob of ``cards_dir`` for ``load_all``, and a ``(max_child_mtime, file_count)`` directory
    stamp for ``revision()``. The ``bootstrap_meta.json`` sidecar and dot-prefixed temp files are
    excluded from every enumeration. Every method is best-effort and never raises.
    """

    def __init__(self, cards_dir: str) -> None:
        self._cards_dir = Path(cards_dir)

    @property
    def cards_dir(self) -> Path:
        """The directory cards live in (created on first write)."""
        return self._cards_dir

    def _card_path(self, card_id: str) -> Path:
        return self._cards_dir / f"{card_id}.json"

    def _is_card_file(self, entry: Path) -> bool:
        return (
            entry.suffix == ".json"
            and not entry.name.startswith(".")
            and entry.name != _BOOTSTRAP_META_FILE
        )

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all card JSON files from cards_dir. Returns ``{card_id: card_dict}``; ``{}`` on error.

        A card whose JSON is corrupt or unreadable is skipped (never aborts the load). The card id is
        the card's own ``id`` field, falling back to the file stem.
        """
        cards: Dict[str, Dict[str, Any]] = {}
        try:
            if not self._cards_dir.exists():
                return cards
            for entry in self._cards_dir.iterdir():
                if not self._is_card_file(entry):
                    continue
                try:
                    with open(entry, "r", encoding="utf-8") as fh:
                        card = json.load(fh)
                    card_id = card.get("id") or entry.stem
                    cards[card_id] = card
                except Exception:  # noqa: BLE001 — corrupt card: skip
                    continue
        except Exception:  # noqa: BLE001
            return {}
        return cards

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Return the card dict stored under ``card_id``, or ``None`` if absent/corrupt/unreadable."""
        try:
            card_path = self._card_path(card_id)
            if not card_path.exists():
                return None
            with open(card_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            return loaded if isinstance(loaded, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Atomically upsert ``card`` (temp file + ``os.replace``). Returns ``True`` on success.

        Creates ``cards_dir`` on first write. On any failure the temp file is cleaned up and
        ``False`` is returned (never raises).
        """
        try:
            self._cards_dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._cards_dir), prefix=".tmp_", suffix=".json"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(card, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp_path, str(self._card_path(card_id)))
                return True
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return False
        except Exception:  # noqa: BLE001
            return False

    def delete(self, card_id: str) -> bool:
        """Delete the card file for ``card_id``. Returns ``True`` when no card remains afterwards."""
        try:
            card_path = self._card_path(card_id)
            if not card_path.exists():
                return True
            try:
                card_path.unlink()
                return True
            except OSError:
                return False
        except Exception:  # noqa: BLE001
            return False

    def exists(self, card_id: str) -> bool:
        """Return ``True`` when a card file exists for ``card_id``."""
        try:
            return self._card_path(card_id).exists()
        except Exception:  # noqa: BLE001
            return False

    def revision(self) -> Tuple[float, int]:
        """Cheap snapshot of cards_dir state: ``(max_child_mtime, file_count)``.

        Detects external writes (other agents / processes) without reading every card. Returns
        ``(0.0, 0)`` if the directory does not exist or cannot be stat-ed.
        """
        try:
            if not self._cards_dir.exists():
                return (0.0, 0)
            max_mtime = 0.0
            count = 0
            for entry in self._cards_dir.iterdir():
                if self._is_card_file(entry):
                    count += 1
                    try:
                        mt = entry.stat().st_mtime
                        if mt > max_mtime:
                            max_mtime = mt
                    except OSError:
                        pass
            return (max_mtime, count)
        except Exception:  # noqa: BLE001
            return (0.0, 0)
