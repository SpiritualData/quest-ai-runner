"""CachedDbAdapter — live DB reads through a short-TTL cache (a RetrievalAdapter).

The "don't sync Mongo to files" approach from the build plan: the brain grounds on LIVE team/
org/quest data by issuing many small ``query`` reads, which this adapter serves from a short-TTL
in-memory cache so the parallel fan-out is fast and cheap. Nothing is synced to disk; the DB is
read where it lives.

It is generic over the actual store: the consumer supplies a ``fetch(collection, filter)``
callable (e.g. a Mongo ``find`` wrapper, or a REST call). This adapter adds:
  * a TTL cache keyed on (collection, canonical-filter), so repeated reads within the window are
    free and the parallel gather doesn't hammer the DB;
  * thread-safety (the brain's gather runs reads concurrently);
  * the RetrievalAdapter shape — ``query`` is the primary surface; ``read_section`` and ``grep``
    map onto queries where it makes sense, else return a clear "unsupported" Observation.

A ``query`` spec is a dict like ``{"collection": "quests", "filter": {"team_id": "..."}, "limit": 20}``.
The Observation's ``text`` is a compact JSON-ish rendering the planner can read; ``rel_path`` is
set to ``collection`` for display.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase

FetchFn = Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]


def _canonical(filter_: Dict[str, Any]) -> str:
    try:
        return json.dumps(filter_ or {}, sort_keys=True, default=str)
    except TypeError:
        return repr(filter_)


class CachedDbAdapter(RetrievalAdapterBase):
    def __init__(self, fetch: FetchFn, *, ttl_seconds: float = 30.0, default_limit: int = 20,
                 max_render_bytes: int = 6000):
        self._fetch = fetch
        self.ttl = ttl_seconds
        self.default_limit = default_limit
        self.max_render_bytes = max_render_bytes
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def _cached_fetch(self, collection: str, filter_: Dict[str, Any]) -> List[Dict[str, Any]]:
        key = f"{collection}:{_canonical(filter_)}"
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < self.ttl:
                return entry[1]
        # Fetch outside the lock so concurrent reads of DIFFERENT keys don't serialize.
        rows = self._fetch(collection, filter_) or []
        with self._lock:
            self._cache[key] = (now, rows)
        return rows

    def invalidate(self, collection: Optional[str] = None) -> None:
        with self._lock:
            if collection is None:
                self._cache.clear()
            else:
                for k in [k for k in self._cache if k.startswith(f"{collection}:")]:
                    del self._cache[k]

    def query(self, spec: Dict[str, Any]) -> Observation:
        if not isinstance(spec, dict):
            return Observation(kind="error", error="query spec must be an object")
        collection = spec.get("collection")
        if not collection:
            return Observation(kind="error", error="query requires 'collection'")
        filter_ = spec.get("filter") or {}
        limit = int(spec.get("limit") or self.default_limit)
        try:
            rows = self._cached_fetch(str(collection), filter_)
        except Exception as e:  # noqa: BLE001 — a DB hiccup must never break the gather loop
            return Observation(kind="error", rel_path=str(collection),
                               error=f"db read failed: {type(e).__name__}")
        rows = rows[:limit]
        try:
            text = json.dumps(rows, indent=2, default=str)
        except TypeError:
            text = repr(rows)
        if len(text.encode("utf-8")) > self.max_render_bytes:
            text = text.encode("utf-8")[: self.max_render_bytes].decode("utf-8", errors="ignore") \
                + "\n…[truncated]"
        return Observation(kind="query", rel_path=str(collection),
                           locator=f"filter={_canonical(filter_)}", text=text)

    # read_section / grep: best-effort mappings onto query so the brain's read action still works.
    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None,
                     max_bytes=None) -> Observation:
        # Treat rel_path as a collection name; no sub-document slicing for a DB read.
        return self.query({"collection": rel_path, "filter": {}})

    def grep(self, pattern, *, scope=None, max_hits=None) -> Observation:
        return Observation(
            kind="error", pattern=pattern, scope=scope,
            error="grep is not supported on a DB source; use a structured query instead")
