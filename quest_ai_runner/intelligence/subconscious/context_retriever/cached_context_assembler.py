"""A short process-wide memory of context assemblies: the same request, in the same scope, within a minute, is not assembled again.

The turn-start assembly is the longest wait of an information turn (queries, searches, filters, consolidation). An identical request
inside the window (a retry, a re-ask, a re-run of the same input) reuses the result. Anything the runner records or learns clears the
memory, so a turn never reads context older than its own writes. The key is the request text and the scope it was assembled for; the
per-call deadline and the cheap-path flag are not part of it except that a cheap result is never served to a full request.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from hubofhubs.api import AssembledContext

#: Seconds an assembly stays reusable, and how many are kept.
TTL_SECONDS = 60.0
MAX_ENTRIES = 32
IGNORED_META = frozenset({"assembly_deadline"})


def cache_key(task_text: str, meta: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
    kept = sorted((str(k), repr(v)) for k, v in (meta or {}).items() if k not in IGNORED_META)
    return (task_text, tuple(kept))


class CachedContextAssembler:
    """Wraps any context assembler; every other attribute is the wrapped assembler's own."""

    def __init__(self, inner: Any, *, ttl: float = TTL_SECONDS, clock: Any = time.monotonic) -> None:
        self.wrapped_assembler = inner
        self.cache_ttl = ttl
        self.cache_clock = clock
        self.cache_lock = threading.Lock()
        self.cache_entries: "OrderedDict[Tuple[Any, ...], Tuple[float, Any]]" = OrderedDict()

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, **kwargs: Any) -> AssembledContext:
        key = cache_key(task_text, meta)
        now = self.cache_clock()
        with self.cache_lock:
            hit = self.cache_entries.get(key)
            if hit is not None and now - hit[0] <= self.cache_ttl:
                self.cache_entries.move_to_end(key)
                return hit[1]
        result = self.wrapped_assembler.assemble(task_text, meta=meta, **kwargs)
        if not getattr(result, "partial", False) and (getattr(result, "context_view", "") or "").strip():
            with self.cache_lock:
                self.cache_entries[key] = (now, result)
                while len(self.cache_entries) > MAX_ENTRIES:
                    self.cache_entries.popitem(last=False)
        return result

    def clear(self) -> None:
        with self.cache_lock:
            self.cache_entries.clear()

    def record(self, *args: Any, **kwargs: Any) -> Any:
        self.clear()
        return self.wrapped_assembler.record(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped_assembler, name)
