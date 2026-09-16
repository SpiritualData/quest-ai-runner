"""StateStore -- JSON-backed signature dedup store, shared by every executor lane.

Extracted from ``runner/poller.py`` (mechanical extraction, no behavior change -- see
``tests/test_statestore_edge_cases.py`` and ``tests/test_state_store_extraction.py``) so a second
lane (``runner/channel_runner.py``, live two-way channels) can dedup inbound messages the SAME
way the task poller dedups due tasks, without a second implementation. ``poller.py`` re-exports
``StateStore`` from here so existing imports (``from quest_ai_runner.runner.poller import
StateStore``) keep working unchanged.
"""
from __future__ import annotations

import datetime as _dt
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
        # Tasks this runner has CLAIMED and not yet finished, persisted so a process that dies
        # mid-run leaves a record of what it was holding. See ``take_orphans``.
        self._in_flight: Dict[str, str] = {}
        # Snapshot of ``_in_flight`` as it was found ON DISK at startup: whatever a PREVIOUS
        # process claimed and never released, i.e. exactly the work a crash abandoned.
        self._orphans: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                # Backward compatible: an existing file's "handled" list becomes the dict's keys,
                # in the same (oldest-first) order they were written.
                self._handled = dict.fromkeys(data.get("handled", []))
                stored = data.get("in_flight") or {}
                if isinstance(stored, dict):
                    # Anything still recorded belongs to a process that is no longer running --
                    # this one is only starting now. Keep it as orphans for the caller to
                    # reconcile, and clear the live set so we never "release" another life's work.
                    self._orphans = {str(k): str(v) for k, v in stored.items()}
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
            payload = json.dumps({"handled": recent, "in_flight": self._in_flight}, indent=2)
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

    # --- crash recovery: what this runner was holding when it died ----------------------------

    def claim_in_flight(self, task_id: str) -> None:
        """Record that this runner has claimed ``task_id`` and is about to run it.

        Written through to disk immediately, because the only case this exists for is the one
        where the process does not get to run any more code: an OOM kill, a SIGKILL, a power cut.
        """
        if not task_id:
            return
        with self._lock:
            self._in_flight[str(task_id)] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            self._save()

    def release_in_flight(self, task_id: str) -> None:
        """Record that ``task_id`` reached a terminal state (or failed loudly). Idempotent."""
        if not task_id:
            return
        with self._lock:
            if self._in_flight.pop(str(task_id), None) is not None:
                self._save()

    def take_orphans(self) -> Dict[str, str]:
        """``{task_id: claimed_at}`` abandoned by a previous process, consumed once.

        WHY THIS EXISTS. Claiming a task PATCHes it to ``in_progress`` on the backend, which is
        what stops another worker taking it. That is correct right up until the worker dies: the
        row then says "someone is running this" and nobody is. Nothing on the runner side noticed,
        because the code that would have noticed is the code that was killed. The task sat until a
        backend sweeper timed it out (hours later) and marked it ``failed`` -- and a failed row is
        not mailable, so for an autopilot quest the whole day's work vanished with no output and no
        error anywhere the person could see. Three consecutive days went missing that way before
        anyone could tell autopilot was even involved.

        Consumed once: the returned ids are cleared from the store, so a task that cannot be
        recovered is not retried forever on every restart.
        """
        with self._lock:
            orphans = dict(self._orphans)
            self._orphans = {}
            if orphans:
                self._save()
            return orphans
