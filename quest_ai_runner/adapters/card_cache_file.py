"""FileCardCache: a durable ``CardCache`` for ``QuestApiCardRepository``, kept as two JSON files in one
directory (``listing.json`` and ``outbox.json``). It survives a process restart, so a lane that starts
while the card backend is unreachable still serves the last listing it saw, and a write that could not
reach the backend is kept, oldest first, for a later replay.

Writes are atomic (temp file then ``os.replace``) so a crash never leaves a half-written file, and every
method swallows its own I/O errors: a cache problem must never break the repository (the repository
never depends on its cache for correctness). Safe to share across threads in one process.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

LISTING_FILE = "listing.json"
OUTBOX_FILE = "outbox.json"


class FileCardCache:
    """A ``CardCache`` persisted under ``directory`` (created on first write)."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.lock = threading.RLock()

    def write_json(self, name: str, payload: Any) -> bool:
        """Atomically write ``payload`` to ``directory/name``. False (logged) on any failure."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, self.directory / name)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True
        except (OSError, TypeError, ValueError):
            _log.warning("card cache: could not write %s", name, exc_info=True)
            return False

    def read_json(self, name: str) -> Any:
        """The parsed file, or None when it is missing or unreadable."""
        try:
            with open(self.directory / name, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            _log.warning("card cache: could not read %s", name, exc_info=True)
            return None

    def load_listing(self) -> Optional[Tuple[Dict[str, Dict[str, Any]], float]]:
        with self.lock:
            data = self.read_json(LISTING_FILE)
        if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
            return None
        try:
            return data["cards"], float(data.get("max_updated_at") or 0.0)
        except (TypeError, ValueError):
            return None

    def store_listing(self, cards: Dict[str, Dict[str, Any]], max_updated_at: float) -> None:
        with self.lock:
            self.write_json(LISTING_FILE, {"max_updated_at": max_updated_at, "cards": cards})

    def enqueue_write(self, card_id: str, card: Dict[str, Any], team_id: str) -> bool:
        """Queue a failed write; a newer write of the same card replaces the queued one in place."""
        with self.lock:
            queue = self.read_json(OUTBOX_FILE)
            queue = queue if isinstance(queue, list) else []
            entry = {"card_id": card_id, "card": card, "team_id": team_id}
            for i, held in enumerate(queue):
                if isinstance(held, dict) and held.get("card_id") == card_id and held.get("team_id") == team_id:
                    queue[i] = entry
                    break
            else:
                queue.append(entry)
            return self.write_json(OUTBOX_FILE, queue)

    def pending_writes(self) -> List[Tuple[str, Dict[str, Any], str]]:
        with self.lock:
            queue = self.read_json(OUTBOX_FILE)
        if not isinstance(queue, list):
            return []
        return [(str(e["card_id"]), e.get("card") or {}, str(e.get("team_id") or ""))
                for e in queue if isinstance(e, dict) and "card_id" in e]
