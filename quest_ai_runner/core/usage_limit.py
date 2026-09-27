"""Claude subscription usage limits: recognise one, read its reset time, remember it lane-wide.

When a keyless (subscription) Claude Code call runs out of allowance it does not fail the way an
outage does. ``claude -p`` exits 1 and its JSON envelope's ``result`` is a synthetic message such
as::

    You've hit your weekly limit · resets Sep 26, 1pm (America/Los_Angeles)
    You've hit your weekly limit · resets 1pm (America/Los_Angeles)
    You've hit your session limit · resets 8:30am (America/Los_Angeles)
    You've hit your monthly spend limit · raise it at claude.ai/settings/usage?... · your weekly
        limit resets 1pm (America/Los_Angeles)

(measured from real session transcripts, 2026-09-22 to 26). The session record the run leaves
behind carries the same moment structurally: the synthetic assistant message has
``"error": "rate_limit"``, ``"isApiErrorMessage": true`` and ``"quotaLimits": {"status":
"rejected", "resetsAt": <epoch seconds>, "rateLimitType": "seven_day" | "five_hour"}``. Older CLI
builds printed ``Claude AI usage limit reached|<epoch>``; that shape is recognised too.

Before this module that text was just "the run's output": a deep run reported it as a failed goal,
a shallow planner call swallowed it, and the lane went on claiming work, burning through its whole
queue in seconds and marking every task failed (seen on the SD shared lane, 2026-09-25). Now:

* ``detect_usage_limit`` recognises the message (only from an ERROR path, never from ordinary
  worker output, which may well discuss limits) and parses when it resets;
* ``limit_from_session_file`` reads the structured ``quotaLimits`` from the run's own session
  record, which beats any text parse;
* ``record`` / ``active_limit`` keep ONE process-wide note of the limit, so the lane can stop
  claiming new Claude work until the reset (the "lane pause"), persisted to disk when the lane
  configures a path so a restart does not forget it;
* ``UsageLimitError`` is what a provider raises so callers can tell this apart from a real error.

The reset instant is used as a start time, so it is always an aware UTC datetime, with a small
grace (``RESUME_GRACE``) added when resuming, since the allowance flips at the reset and not before.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("quest-ai-runner.usage-limit")

# How long after the stated reset to resume: the allowance flips AT the reset, so a claim at the
# exact minute can still be refused.
RESUME_GRACE = timedelta(minutes=2)
# When Claude Code said it is limited but no reset time could be read at all: wait this long,
# doubling per consecutive hit, never more than the cap. Each resumed attempt is itself the probe.
UNPARSED_BACKOFF = timedelta(minutes=30)
UNPARSED_BACKOFF_CAP = timedelta(hours=3)
# A reset stated without a date that parses to more than this far ahead is not trusted (it would
# be tomorrow at the earliest for a clock time, a week at most for a weekly limit).
MAX_PLAUSIBLE_WAIT = timedelta(days=8)

# Which messages ARE a usage limit. Each must be specific to Claude Code's own wording, since the
# same text search runs over error output that can contain arbitrary words.
_LIMIT_PATTERNS = (
    re.compile(r"\bhit your (?P<kind>[\w\- ]{0,30}?)\s*limit\b", re.IGNORECASE),
    re.compile(r"\bClaude AI usage limit reached\b", re.IGNORECASE),
    re.compile(r"\b(?P<kind>5-hour|weekly|session|daily|opus|sonnet)\s+limit reached\b", re.IGNORECASE),
    re.compile(r"\busage limit reached\b", re.IGNORECASE),
)
_LEGACY_EPOCH = re.compile(r"usage limit reached\|(?P<epoch>\d{9,11})", re.IGNORECASE)
_RESETS = re.compile(
    r"resets\s+(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_\-+0-9]+)*)\))?",
    re.IGNORECASE,
)
_RESETS_IN = re.compile(r"resets\s+in\s+(?:(?P<h>\d+)\s*h(?:ours?|rs?)?)?\s*(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?)?",
                        re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_RATE_LIMIT_TYPES = {"seven_day": "weekly", "five_hour": "session", "seven_day_opus": "weekly Opus"}


@dataclass
class UsageLimit:
    """One observed usage limit."""
    message: str                                  # Claude Code's own words, trimmed
    kind: str = ""                                # "weekly", "session", "monthly spend", ...
    resets_at: Optional[datetime] = None          # aware UTC, None when it could not be read
    seen_at: float = field(default_factory=time.time)

    def resume_at(self, hit_count: int = 1, now: Optional[datetime] = None) -> datetime:
        """When work may be tried again: the reset plus grace, or a capped backoff when unknown."""
        now = now or datetime.now(timezone.utc)
        if self.resets_at is not None and self.resets_at > now - RESUME_GRACE:
            return self.resets_at + RESUME_GRACE
        steps = max(0, int(hit_count or 1) - 1)
        wait = min(UNPARSED_BACKOFF * (2 ** min(steps, 8)), UNPARSED_BACKOFF_CAP)
        return now + wait

    def label(self) -> str:
        """"weekly limit", "session limit", or plain "usage limit"."""
        kind = (self.kind or "").strip().lower()
        return f"{kind} limit" if kind else "usage limit"

    def to_dict(self) -> Dict[str, Any]:
        return {"message": self.message, "kind": self.kind, "seen_at": self.seen_at,
                "resets_at": self.resets_at.isoformat() if self.resets_at else None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["UsageLimit"]:
        try:
            resets = data.get("resets_at")
            return cls(message=str(data.get("message") or ""), kind=str(data.get("kind") or ""),
                       resets_at=datetime.fromisoformat(resets) if resets else None,
                       seen_at=float(data.get("seen_at") or time.time()))
        except (TypeError, ValueError):
            return None


class UsageLimitError(RuntimeError):
    """A Claude call was refused because the subscription usage limit is reached.

    A RuntimeError on purpose: every caller that already catches the provider's RuntimeError keeps
    working unchanged, and the ones that care can catch this narrower type (or read ``.limit``).
    """

    def __init__(self, limit: UsageLimit, detail: Optional[str] = None):
        super().__init__(detail or f"Claude Code usage limit reached: {limit.message}")
        self.limit = limit


def zone(name: Optional[str]):
    """A ZoneInfo for ``name``, else the host's local zone."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo


def parse_reset_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """The reset instant in a limit message, as aware UTC, or None if there is none to read.

    "resets 1pm (America/Los_Angeles)" is the NEXT 1pm in that zone; "resets Sep 26, 1pm (...)"
    is that date (next year if it would otherwise be well in the past); "resets in 2h 30m" is
    relative. A zone the host cannot resolve falls back to the host's own zone.
    """
    now = now or datetime.now(timezone.utc)
    if not text:
        return None
    legacy = _LEGACY_EPOCH.search(text)
    if legacy:
        try:
            return datetime.fromtimestamp(int(legacy.group("epoch")), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    rel = _RESETS_IN.search(text)
    if rel and (rel.group("h") or rel.group("m")):
        return now + timedelta(hours=int(rel.group("h") or 0), minutes=int(rel.group("m") or 0))
    match = _RESETS.search(text)
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm").lower() == "pm":
        hour += 12
    minute = int(match.group("minute") or 0)
    if minute > 59:
        return None
    tz = zone(match.group("tz"))
    local_now = now.astimezone(tz)
    if match.group("mon"):
        month = _MONTHS.get(match.group("mon")[:3].lower())
        if not month:
            return None
        try:
            candidate = local_now.replace(month=month, day=int(match.group("day")), hour=hour,
                                          minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if candidate < local_now - timedelta(days=180):
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
    result = candidate.astimezone(timezone.utc)
    if result - now > MAX_PLAUSIBLE_WAIT:
        return None
    return result


def detect_usage_limit(text: Optional[str], now: Optional[datetime] = None) -> Optional[UsageLimit]:
    """A ``UsageLimit`` when ``text`` is Claude Code's usage-limit message, else None.

    Call this only on an ERROR path (a non-zero exit, an ``is_error`` envelope, a raised provider
    error). Ordinary worker output may mention limits; this only ever reads what the CLI said when
    it refused, which is short. Anything long is not that message, so it is not scanned past its
    first few hundred characters.
    """
    if not text:
        return None
    head = str(text).strip()[:600]
    for pattern in _LIMIT_PATTERNS:
        match = pattern.search(head)
        if match:
            kind = ""
            if "kind" in pattern.groupindex and match.group("kind"):
                kind = match.group("kind").strip().lower()
            message = head.splitlines()[0][:300] if head else ""
            return UsageLimit(message=message, kind=kind, resets_at=parse_reset_time(head, now))
    return None


def limit_from_session_file(path: Optional[Path]) -> Optional[UsageLimit]:
    """The usage limit recorded at the END of a Claude Code session file, if it ended on one.

    Reads the last few records only. Structured and exact (``quotaLimits.resetsAt`` is epoch
    seconds), so it wins over any text parse when the file is there. Never raises.
    """
    if not path:
        return None
    try:
        p = Path(path)
        if not p.is_file():
            return None
        with p.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail[-40:]):
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        if not record.get("isApiErrorMessage") or record.get("error") != "rate_limit":
            # The session's last word was not a limit refusal: it did not end on one.
            return None
        quota = record.get("quotaLimits") or {}
        content = (record.get("message") or {}).get("content") or []
        text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict)).strip()
        found = detect_usage_limit(text) or UsageLimit(message=text or "Claude Code usage limit reached")
        resets = quota.get("resetsAt")
        if isinstance(resets, (int, float)) and resets > 0:
            found.resets_at = datetime.fromtimestamp(float(resets), tz=timezone.utc)
        if not found.kind and quota.get("rateLimitType") in _RATE_LIMIT_TYPES:
            found.kind = _RATE_LIMIT_TYPES[quota["rateLimitType"]]
        return found
    return None


# --- the lane-wide note ------------------------------------------------------------------------

_lock = threading.Lock()
_current: Optional[UsageLimit] = None
_persist_path: Optional[Path] = None


def configure_persistence(path: Optional[str]) -> None:
    """Remember the lane's limit in ``path`` (JSON) so a restarted lane still honours it."""
    global _persist_path, _current
    with _lock:
        _persist_path = Path(path) if path else None
        if _persist_path and _persist_path.is_file():
            try:
                loaded = UsageLimit.from_dict(json.loads(_persist_path.read_text()))
            except (OSError, ValueError):
                loaded = None
            if loaded and (_current is None or loaded.seen_at > _current.seen_at):
                _current = loaded


def record(limit: UsageLimit) -> UsageLimit:
    """Note that Claude Code is at its usage limit. Returns the limit now in force.

    Keeps the later reset when two concurrent runs report slightly different ones.
    """
    global _current
    with _lock:
        # seen_at is when the LANE learned of it, which is what "seen during this run" measures.
        limit.seen_at = time.time()
        if (_current is not None and _current.resets_at and limit.resets_at
                and _current.resets_at > limit.resets_at and _is_active(_current)):
            _current.seen_at = max(_current.seen_at, limit.seen_at)
        else:
            _current = limit
        log.warning("Claude Code usage limit reached (%s); resets %s", _current.label(),
                    _current.resets_at.isoformat() if _current.resets_at else "at an unknown time")
        if _persist_path is not None:
            try:
                _persist_path.parent.mkdir(parents=True, exist_ok=True)
                _persist_path.write_text(json.dumps(_current.to_dict()))
            except OSError:
                log.warning("could not persist the usage limit to %s", _persist_path, exc_info=True)
        return _current


def _is_active(limit: UsageLimit, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if limit.resets_at is not None:
        return now < limit.resets_at + RESUME_GRACE
    return now.timestamp() < limit.seen_at + UNPARSED_BACKOFF.total_seconds()


def active_limit(now: Optional[datetime] = None) -> Optional[UsageLimit]:
    """The limit the lane is currently paused on, or None when Claude Code should be available."""
    with _lock:
        if _current is not None and _is_active(_current, now):
            return _current
        return None


def pause_until(now: Optional[datetime] = None) -> Optional[datetime]:
    """When the lane pause ends, or None when not paused."""
    limit = active_limit(now)
    return limit.resume_at(now=now) if limit else None


def seen_since(started: float) -> Optional[UsageLimit]:
    """The limit, if one was recorded at or after ``started`` (a ``time.time()`` value)."""
    with _lock:
        if _current is not None and _current.seen_at >= started:
            return _current
        return None


def from_exception(exc: BaseException) -> Optional[UsageLimit]:
    """The usage limit behind an exception, if that is what it is (walks the cause chain)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        limit = getattr(exc, "limit", None)
        if isinstance(limit, UsageLimit):
            return limit
        found = detect_usage_limit(str(exc))
        if found:
            return found
        exc = exc.__cause__ or exc.__context__
    return None


def clear(reason: str = "") -> None:
    """Forget the current limit now (a person released a task: try Claude Code for real).

    If the limit is still in force the very next call records it again, so this can never cause
    more than one refused attempt.
    """
    global _current
    with _lock:
        if _current is None:
            return
        _current = None
        log.info("usage-limit pause lifted early%s", f" ({reason})" if reason else "")
        if _persist_path is not None:
            try:
                _persist_path.unlink(missing_ok=True)
            except OSError:
                pass


def reset_for_tests() -> None:
    """Forget any recorded limit (tests only)."""
    global _current, _persist_path
    with _lock:
        _current = None
        _persist_path = None
