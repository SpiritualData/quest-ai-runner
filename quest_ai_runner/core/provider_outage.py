"""Model-provider outages: recognise one, remember the latest one, and wait it out.

A model provider that is overloaded, throttling, or briefly down is an environment failure, not a
fault in the task. Before this module a queued task hit one, the provider's own retries ran out in
a few seconds, and the task was reported failed ("failed after 1 attempt"), so the person had to
come back and run it again by hand.

Now the executor puts the task back in the queue with ``start_at`` set a few minutes ahead and
``hold_reason=provider_unavailable``. The backend keeps every lane off the task until then, so the
task waits visibly and retries by itself. Each retry is a fresh probe, and nothing is lost: the
task was never run to a result.

* ``is_outage`` recognises the failure from its message (status codes and provider wording, never
  a bare "timeout", which also means a slow backend or a deep run that ran out of wall clock);
* ``from_exception`` checks the raised error and the chain it was raised from;
* ``note`` remembers the last outage the provider's retry wrapper gave up on, so a run that
  returned a failed result (rather than raising) can still be attributed to the outage;
* ``seen_since`` reports an outage recorded during one run, so a concurrent run's outage is never
  blamed on this one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("quest-ai-runner.provider-outage")

# How long a provider-unavailable task waits before its next try. Short enough that a brief
# overload costs little, long enough that a long outage does not burn a claim every poll.
RETRY_INTERVAL = timedelta(minutes=10)

# Provider wording for "not available right now". Deliberately no bare "timeout" or "connection":
# those also mean a slow backend or a dropped socket on the Quest API, which a wait does not fix.
_OUTAGE_MARKERS = (
    "503", "529", "502", "504", "429",
    "overloaded", "service unavailable", "temporarily unavailable", "bad gateway",
    "gateway timeout", "internal server error", "resource_exhausted", "resource exhausted",
    "rate limit", "ratelimit",
)


@dataclass(frozen=True)
class ProviderOutage:
    message: str
    at: float  # time.time() when it was recorded


_lock = threading.Lock()
_last: Optional[ProviderOutage] = None


def is_outage(exc: BaseException) -> bool:
    """True when a failed model call says the provider is unavailable right now."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _OUTAGE_MARKERS)


def _describe(exc: BaseException) -> str:
    detail = str(exc).strip()[:300]
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def from_exception(exc: BaseException) -> Optional[ProviderOutage]:
    """The outage an exception stands for (itself or anything it was raised from), else None."""
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if is_outage(current):
            return ProviderOutage(message=_describe(current), at=time.time())
        current = current.__cause__ or current.__context__
    return None


def note(exc: BaseException) -> None:
    """Remember ``exc`` as the latest provider outage, if it is one. Never raises."""
    global _last
    outage = from_exception(exc)
    if outage is None:
        return
    with _lock:
        _last = outage
    log.warning("model provider unavailable: %s", outage.message)


def seen_since(moment: float) -> Optional[ProviderOutage]:
    """The latest outage recorded at or after ``moment`` (a ``time.time()`` value), else None."""
    with _lock:
        last = _last
    if last is not None and last.at >= moment:
        return last
    return None


def resume_at(now: Optional[datetime] = None) -> datetime:
    """When a task held for a provider outage is next tried."""
    return (now or datetime.now(timezone.utc)) + RETRY_INTERVAL


def reset_for_tests() -> None:
    global _last
    with _lock:
        _last = None
