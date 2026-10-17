"""web_research, the daily cost guard: ``DailyLimiter`` counts REAL, PAID calls per UTC day (backend
searches and the Gemini ``url_fetch_fallback``), shared across processes through an advisory file
lock when the platform has one.

``adapters.web_research`` re-exports it.
"""
from __future__ import annotations

import logging
import json
import os
import tempfile
import threading
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..core.file_modes import match_umask

logger = logging.getLogger("quest-ai-runner.web-research")

#: Bounded wait for the cross-process advisory lock, so a contended lock degrades to "proceed
#: without it" rather than hanging a turn.
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_INTERVAL_SECONDS = 0.05

try:
    import fcntl  # type: ignore
except ImportError:  # Windows: no advisory file locking.
    fcntl = None  # type: ignore[assignment]


class DailyLimiter:
    """UTC-day counter of REAL, PAID calls: backend searches and ``url_fetch_fallback`` (Gemini
    url_context) fetches that are actually issued. Cache hits and direct HTML fetches never
    count; they are free.

    Thread-safe within one process. When ``state_dir`` is set, the persisted file (not the
    in-memory count) is the source of truth ACROSS processes: every other process pointed at the
    same ``QAR_WEB_CACHE_DIR`` shares one spend. ``record()`` takes an exclusive advisory lock,
    re-reads the file, rolls the day, increments, and writes atomically, so concurrent processes
    (two runner lanes, a terminal session and a web backend, all sharing one search API quota)
    add up instead of each overwriting the other's count with its own stale in-memory value --
    the bug this replaces undercounted by up to the number of processes sharing the directory.
    ``exhausted()`` re-reads the file too (a few hundred bytes), so one process sees another's
    spend, without taking a lock (see ``exhausted`` for why a plain read is safe here).

    A limit of ``None``/``0`` disables the guard entirely (no file I/O either). Without
    ``state_dir`` behavior is unchanged: a plain in-memory, per-process counter.
    """

    def __init__(self, limit: Optional[int], *, state_dir: Optional[Path] = None) -> None:
        self._limit = int(limit) if limit else 0
        self._state_path = (state_dir / "web_search_daily_count.json") if state_dir else None
        self._lock_path = (
            (state_dir / "web_search_daily_count.lock") if state_dir else None
        )
        self._lock = threading.Lock()
        self._day = self._today()
        self._count = 0
        if self._state_path is not None:
            self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> None:
        """Read the persisted count into the in-memory cache. Best-effort: a missing or corrupt
        file reads as 0 (today) rather than raising, and gets repaired on the next ``record()``."""
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            day = raw.get("day")
            count = int(raw.get("count", 0))
        except (OSError, ValueError, KeyError, TypeError):
            return
        if day == self._today():
            self._day = day
            self._count = count
        # A stale day on disk is not rolled here: the in-memory default (today, 0) already
        # reflects a fresh day, and the next record() will roll-and-write it for real.

    def _save_locked(self) -> None:
        """Atomic write of the in-memory ``(day, count)``. Caller holds ``self._lock`` and,
        when persisted, the cross-process file lock."""
        if self._state_path is None:
            return
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_path.parent), prefix=".webcount-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"day": self._day, "count": self._count}, fh)
                fh.flush()
                match_umask(fh.fileno())
            os.replace(tmp_path, self._state_path)
            tmp_path = None
        except OSError:
            logger.debug("web search daily limiter: could not persist count", exc_info=True)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _roll_day_locked(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._count = 0

    def _acquire_cross_process_lock(self):
        """Open (creating if needed) and exclusively lock the sidecar lock file, bounded by
        ``LOCK_TIMEOUT_SECONDS``. Returns the open fd on success, or ``None`` if locking is
        unavailable (no ``fcntl``, e.g. Windows) or the lock could not be acquired in time --
        callers must proceed without it rather than block a turn."""
        if fcntl is None or self._lock_path is None:
            return None
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o666)
            match_umask(fd)
        except OSError:
            logger.debug("web search daily limiter: could not open lock file", exc_info=True)
            return None
        deadline = datetime.now(timezone.utc).timestamp() + LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    if datetime.now(timezone.utc).timestamp() >= deadline:
                        logger.debug(
                            "web search daily limiter: lock acquisition timed out; "
                            "proceeding without it"
                        )
                        os.close(fd)
                        return None
                    time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        except Exception:  # noqa: BLE001
            try:
                os.close(fd)
            except OSError:
                pass
            return None

    @staticmethod
    def _release_cross_process_lock(fd: Optional[int]) -> None:
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def exhausted(self) -> bool:
        """Whether today's count has reached the limit. When persisted, re-reads the file under
        NO lock: the file is only ever replaced via ``os.replace`` (atomic on POSIX), so a plain
        read always sees either the previous complete write or the next one, never a partial
        one -- the simpler option, and correct, so a second lock here would only add contention
        for no safety benefit. Falls back to the in-memory count if the file is missing, corrupt,
        or (when locking is unavailable) simply not re-read.
        """
        if self._limit <= 0:
            return False
        with self._lock:
            self._roll_day_locked()
            if self._state_path is not None:
                self._load()
            return self._count >= self._limit

    def record(self) -> None:
        """Record one real, paid call (a backend search, or a url_fetch_fallback fetch that was
        actually issued). No-op when the guard is disabled.

        When persisted, the FILE is the source of truth across processes: this takes an exclusive
        cross-process lock, re-reads the latest count, rolls the day if the stored day is stale,
        increments, and writes atomically before releasing the lock -- so two processes recording
        concurrently both land (never one clobbering the other's increment). Never raises: a lock
        that can't be acquired in time (no ``fcntl``, or genuine contention) degrades to the old
        in-process-only behavior for this call rather than blocking the turn.
        """
        if self._limit <= 0:
            return
        with self._lock:
            lock_fd = self._acquire_cross_process_lock() if self._state_path is not None else None
            try:
                if lock_fd is not None:
                    # Another process may have advanced the count (or rolled the day) since our
                    # last read; the lock guarantees this read is current.
                    self._load()
                self._roll_day_locked()
                self._count += 1
                self._save_locked()
            finally:
                self._release_cross_process_lock(lock_fd)
