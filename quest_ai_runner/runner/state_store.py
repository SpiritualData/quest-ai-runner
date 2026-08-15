"""StateStore -- JSON-backed signature dedup store, shared by every executor lane.

Extracted from ``runner/poller.py`` (mechanical extraction, no behavior change -- see
``tests/test_statestore_edge_cases.py`` and ``tests/test_state_store_extraction.py``) so a second
lane (``runner/channel_runner.py``, live two-way channels) can dedup inbound messages the SAME
way the task poller dedups due tasks, without a second implementation. ``poller.py`` re-exports
``StateStore`` from here so existing imports (``from quest_ai_runner.runner.poller import
StateStore``) keep working unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("quest-ai-runner.state_store")


class StateStore:
    """JSON-backed signature store (watchdog_state.json generalized; pluggable backend)."""

    def __init__(self, path: Optional[str]):
        self._path = Path(path) if path else None
        # Insertion-ordered set of handled signatures (dict keys preserve insertion order; values
        # are unused). This lets the save-time cap evict the OLDEST entries first instead of an
        # arbitrary subset (a plain ``set`` has no defined iteration order).
        self._handled: Dict[str, None] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                # Backward compatible: an existing file's "handled" list becomes the dict's keys,
                # in the same (oldest-first) order they were written.
                self._handled = dict.fromkeys(data.get("handled", []))
            except (json.JSONDecodeError, OSError):
                log.warning("state file corrupt/unreadable; starting fresh")

    def _save(self):
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Cap the stored set so it can't grow unbounded over a long-running service. Dict keys
            # preserve insertion order, so this drops the OLDEST entries first (not an arbitrary
            # subset), keeping the most-recently-marked 5000 signatures.
            recent = list(self._handled)[-5000:]
            payload = json.dumps({"handled": recent}, indent=2)
            # Atomic write: write to a temp file in the same directory, then os.replace() so a
            # crash/interruption mid-write can never leave a corrupt/partial state file — the
            # replace is a single filesystem operation.
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(payload)
            os.replace(tmp_path, self._path)
        except OSError as e:
            log.warning("could not persist state: %s", e)

    def seen(self, sig: str) -> bool:
        with self._lock:
            return sig in self._handled

    def mark(self, sig: str):
        with self._lock:
            self._handled[sig] = None
            self._save()
