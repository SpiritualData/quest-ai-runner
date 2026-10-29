"""Per-turn usage accounting scope. Moved to the hub package (``hubofhubs.ports.scope``); this module re-exports every name so the
existing imports keep working. The importers are rewritten seat by seat as seats move, not in one sweep (CHANGELOG).
"""
from __future__ import annotations

from hubofhubs.ports.scope import (  # noqa: F401
    _usage_scope_state,
    current_usage_scope,
    bound_usage_scope,
    ScopedThreadPoolExecutor,
    ScopedCounter,
    ThreadLocalCounter,
)
