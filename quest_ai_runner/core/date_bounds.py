"""Time-range bound helpers for ``filters["time_range"]`` HARD filters over a candidate's timestamp.

Pure stdlib, so ``core`` (recent_context) and the adapters (conversation stores, Claude conversations)
share one implementation. ``adapters.conversation_format`` re-exports both names.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_date_bound(value: Optional[str], *, end_of_day: bool) -> Optional[float]:
    """Parse an ISO date/datetime string (a ``filters["time_range"]`` bound) into a Unix epoch.

    A bare date ("2026-07-09") is the START of that day, or its END (23:59:59) when
    ``end_of_day`` -- so an "end" bound given as just a date still includes the whole day. Returns
    None for missing/unparseable input (an open-ended bound on that side). Never raises."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and len(value.strip()) <= 10:
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    return dt.timestamp()


def timestamp_in_range(ts: float, start: Optional[float], end: Optional[float]) -> bool:
    """True when ``ts`` (epoch seconds) falls within [start, end]; either bound may be open."""
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True
