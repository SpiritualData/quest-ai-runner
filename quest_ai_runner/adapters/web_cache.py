"""WebCache -- thread-safe LRU cache (memory + optional on-disk) for web search/fetch results.

Search results and extracted page text are both expensive (an API call with cost, or a full
HTTP fetch + HTML parse) and highly repeatable (the same question gets asked across many turns,
the same URL gets requested by more than one planner call). This cache sits in front of both so
a repeat lookup is free.

Two tiers:
  * An in-memory ``OrderedDict`` LRU, capped at ``max_entries``, with a per-entry expiry.
  * An OPTIONAL on-disk tier (one JSON file per key, named by a sha256 hash of the cache key) that
    survives process restarts. Writes are atomic (``tempfile.mkstemp`` + ``os.replace``) and the
    file is chmod'd to match what a normal ``open(path, "w")`` would have produced (see
    ``core.file_modes.match_umask``) so a corpus shared by more than one account stays readable by
    both. A corrupt or unreadable disk file is treated as a cache miss, never an error.

Nothing here raises out of the public API: a disk I/O failure degrades to memory-only behavior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from ..core.file_modes import match_umask

logger = logging.getLogger("quest-ai-runner.web-cache")

# Punctuation/quote characters stripped from the ends of a normalized query.
_STRIP_CHARS = " \t\n\r\"'.,;:!?()[]{}"

# A cache entry's filename is "<sha256 hex>.json" and nothing else; see ``_disk_path``.
_ENTRY_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")


def _is_entry_filename(name: str) -> bool:
    """True only for a file this cache itself wrote (so pruning never touches a neighbor's)."""
    return bool(_ENTRY_NAME_RE.match(name))


def normalize_query(query: str) -> str:
    """Lowercase, collapse internal whitespace, and strip surrounding punctuation/quotes.

    Used to build stable cache keys so "What is the weather in Paris?", "what is the weather
    in paris", and "  What is the weather in Paris?  " all hit the same cache entry.
    """
    if not query:
        return ""
    collapsed = " ".join(query.lower().split())
    return collapsed.strip(_STRIP_CHARS)


class WebCache:
    """Thread-safe LRU cache with an optional on-disk persistence tier.

    Parameters
    ----------
    directory:
        Optional directory for the on-disk tier. ``None`` (the default) means memory-only.
    max_entries:
        Max number of entries kept in the in-memory LRU.
    max_disk_entries:
        Soft cap on the number of files kept on disk; pruned (oldest by mtime first) every
        ~50 writes rather than on every write, to keep writes cheap.
    """

    def __init__(
        self,
        directory: Optional[Union[str, "os.PathLike[str]"]] = None,
        max_entries: int = 512,
        max_disk_entries: int = 4000,
    ) -> None:
        self._max_entries = max_entries
        self._max_disk_entries = max_disk_entries
        self._lock = threading.Lock()
        self._mem: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._writes_since_prune = 0

        self._dir: Optional[Path] = None
        if directory is not None:
            try:
                d = Path(directory).resolve()
                d.mkdir(parents=True, exist_ok=True)
                self._dir = d
            except OSError:
                logger.debug("WebCache: could not create cache dir %r", directory, exc_info=True)
                self._dir = None

    @property
    def directory(self) -> Optional[Path]:
        """The on-disk cache directory, or ``None`` when this cache is memory-only."""
        return self._dir

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mem_key(namespace: str, key: str) -> str:
        return f"{namespace}\x1f{key}"

    def _disk_path(self, namespace: str, key: str) -> Optional[Path]:
        if self._dir is None:
            return None
        digest = hashlib.sha256(f"{namespace}\x1f{key}".encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, namespace: str, key: str) -> Optional[Any]:
        """Return the cached value for ``(namespace, key)``, or ``None`` on a miss/expiry."""
        mem_key = self._mem_key(namespace, key)
        now = time.time()

        with self._lock:
            entry = self._mem.get(mem_key)
            if entry is not None:
                expires_at, value = entry
                if expires_at >= now:
                    self._mem.move_to_end(mem_key)
                    self._hits += 1
                    return value
                del self._mem[mem_key]

        path = self._disk_path(namespace, key)
        if path is not None:
            value, expires_at = self._read_disk(path)
            if value is not None or expires_at is not None:
                if expires_at is not None and expires_at >= now:
                    with self._lock:
                        self._mem[mem_key] = (expires_at, value)
                        self._mem.move_to_end(mem_key)
                        self._evict_mem_locked()
                        self._hits += 1
                    return value
                # Expired or corrupt: clean up the stale file.
                try:
                    path.unlink()
                except OSError:
                    pass

        with self._lock:
            self._misses += 1
        return None

    def set(self, namespace: str, key: str, value: Any, ttl_seconds: float) -> None:
        """Store ``value`` for ``(namespace, key)`` for ``ttl_seconds``. Never raises."""
        mem_key = self._mem_key(namespace, key)
        expires_at = time.time() + max(0.0, float(ttl_seconds))

        with self._lock:
            self._mem[mem_key] = (expires_at, value)
            self._mem.move_to_end(mem_key)
            self._evict_mem_locked()

        path = self._disk_path(namespace, key)
        if path is not None:
            self._write_disk(path, expires_at, value)
            self._maybe_prune_disk()

    def stats(self) -> Dict[str, Any]:
        """Return a snapshot of cache counters, safe to log or surface in a status line."""
        with self._lock:
            mem_entries = len(self._mem)
            hits = self._hits
            misses = self._misses

        disk_entries = 0
        if self._dir is not None:
            try:
                disk_entries = sum(
                    1 for p in self._dir.glob("*.json") if _is_entry_filename(p.name)
                )
            except OSError:
                pass

        return {
            "hits": hits,
            "misses": misses,
            "mem_entries": mem_entries,
            "disk_entries": disk_entries,
            "directory": str(self._dir) if self._dir else None,
        }

    # ------------------------------------------------------------------
    # Internal: memory tier
    # ------------------------------------------------------------------

    def _evict_mem_locked(self) -> None:
        """Evict oldest entries over ``max_entries``. Caller must hold ``self._lock``."""
        while len(self._mem) > self._max_entries:
            self._mem.popitem(last=False)

    # ------------------------------------------------------------------
    # Internal: disk tier
    # ------------------------------------------------------------------

    def _read_disk(self, path: Path) -> Tuple[Optional[Any], Optional[float]]:
        """Return ``(value, expires_at)`` from a disk cache file, or ``(None, None)`` on any
        failure (missing file, corrupt JSON, unexpected shape). Never raises."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(raw["expires_at"])
            value = raw["value"]
            return value, expires_at
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.debug("WebCache: corrupt or unreadable cache file %s", path, exc_info=True)
            return None, None

    def _write_disk(self, path: Path, expires_at: float, value: Any) -> None:
        """Atomically write a cache entry to disk. Best-effort: never raises."""
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=".webcache-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"expires_at": expires_at, "value": value}, fh)
                fh.flush()
                match_umask(fh.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        except (OSError, TypeError, ValueError):
            logger.debug("WebCache: failed to write cache file %s", path, exc_info=True)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _maybe_prune_disk(self) -> None:
        """Every ~50 writes, drop the oldest files on disk past ``max_disk_entries``."""
        if self._dir is None:
            return
        with self._lock:
            self._writes_since_prune += 1
            should_prune = self._writes_since_prune >= 50
            if should_prune:
                self._writes_since_prune = 0
        if not should_prune:
            return
        try:
            # Only this cache's OWN files: an entry is named for the sha256 of its key, so
            # anything else in the directory is somebody else's (the daily-limit counter writes
            # ``web_search_daily_count.json`` into this same directory, and deleting that resets
            # the day's cost guard to zero).
            files = sorted(
                (p for p in self._dir.glob("*.json") if _is_entry_filename(p.name)),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        excess = len(files) - self._max_disk_entries
        if excess <= 0:
            return
        for p in files[:excess]:
            try:
                p.unlink()
            except OSError:
                pass
