"""local_time — the ONLY place ``zoneinfo`` is touched in this repo.

Autopilot needs to reason about a person's OWN wall clock (a quest's ``run_time`` +
``run_timezone``), not the runner host's. Every place that needs to do so goes through the four
small functions here instead of touching ``zoneinfo``/``datetime`` tz arithmetic directly, so the
degradation rule stays enforced in exactly one place: an unreadable or missing zone name NEVER
raises and NEVER silently substitutes UTC (a runner in, say, US/Pacific would have every existing
schedule move by 7-8 hours). It degrades to the runner's own local clock, which is the same
fallback ``_due_now_locally`` already used before any of this existed.

See ``quest_autopilot_design.md``'s autopilot spec (section A4) for why UTC is never the fallback,
and C1 for this module's exact contract.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("quest-ai-runner.local_time")

__all__ = ["resolve_zone", "now_in_zone", "today_in_zone", "scheduled_moment"]

# Distinct bad zone names already warned about, so a persistently misconfigured quest logs once
# ever (per process) rather than once per scan forever.
_WARNED_BAD_ZONES: set = set()


def resolve_zone(name: Optional[str]) -> Optional[tzinfo]:
    """``ZoneInfo(name)``, or ``None`` for an empty, unknown, or unloadable name.

    Never raises. An empty/absent name is the ordinary "no zone configured" case and logs
    nothing. A name that fails to resolve (a typo, or a minimal host with no tzdata) logs a
    WARNING once per distinct bad name and degrades quietly on every later call, so a standing
    misconfiguration cannot spam the log once per scan.
    """
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if text not in _WARNED_BAD_ZONES:
            _WARNED_BAD_ZONES.add(text)
            log.warning(
                "autopilot: timezone %r could not be resolved (unknown IANA name, or tzdata is "
                "unavailable on this host) — falling back to the runner's local clock instead",
                text)
        return None


def now_in_zone(name: Optional[str], now: Optional[datetime] = None) -> datetime:
    """The current moment expressed in ``name``, or a local fallback when the zone is unknown.

    ``now`` anchors the computation at a specific instant instead of the wall clock (tests, and a
    single scan comparing every task against one frozen moment). It may be aware (converted with
    ``astimezone``, correct regardless of its own tzinfo) or naive (treated as UTC, matching the
    convention this repo already uses for a naive stored timestamp -- see ``autopilot._parse_dt``).

    Returns an AWARE datetime in ``name`` when the zone resolves. When it does not, returns
    ``now`` unchanged if given, else the naive local ``datetime.now()`` -- the same value
    ``_due_now_locally`` used everywhere before per-quest timezones existed.
    """
    zone = resolve_zone(name)
    if zone is None:
        return now if now is not None else datetime.now()
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is not None:
        return now.astimezone(zone)
    return now.replace(tzinfo=timezone.utc).astimezone(zone)


def today_in_zone(name: Optional[str], now: Optional[datetime] = None) -> date:
    """Today's calendar date in ``name`` (or the local fallback date when the zone is unknown)."""
    return now_in_zone(name, now).date()


def scheduled_moment(date_str: str, time_str: Optional[str],
                      name: Optional[str]) -> Optional[datetime]:
    """``'YYYY-MM-DD'`` + ``'HH:MM'`` read as a wall-clock reading in ``name``.

    Returns an AWARE datetime when ``name`` resolves, a NAIVE one when it does not (the local
    fallback -- comparable directly against a naive ``datetime.now()``), or ``None`` when
    ``date_str``/``time_str`` cannot be parsed at all. An empty ``time_str`` means midnight,
    matching ``_due_now_locally``'s existing convention.
    """
    text = str(date_str or "").strip()
    if not text:
        return None
    clock = str(time_str or "00:00").strip()[:5]
    try:
        naive = datetime.strptime(f"{text} {clock}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    zone = resolve_zone(name)
    if zone is None:
        return naive
    return naive.replace(tzinfo=zone)
