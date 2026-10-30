"""Quest client support: logger, the API error types, shared patterns and limits.

Split out of ``quest_client.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional

from ..core.tools_status import DONE_STATUSES, ToolStatus, classify_payload, decision_id_of


log = logging.getLogger("quest-ai-runner.quest_client")


# Period formats accepted by quest-backend's app/utils/period_utils (day/week/month/quarter/year).
_PERIOD_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{4}_W\d{2}|\d{4}_\d{2}|\d{4}_Q[1-4]|\d{4})$"
)

# A calendar day and a 24-hour clock time: the two halves of a goal's scheduled start.
SCHEDULE_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEDULE_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _as_task_list(resp: Any) -> List[Dict[str, Any]]:
    """Normalize a tasks response to a plain list.

    The Quest backend returns ``{"tasks": [...], "count": N}``; older/mock shapes may return a
    bare ``[...]``. Either way, yield the list of task dicts (``[]`` when absent)."""
    if isinstance(resp, dict):
        return list(resp.get("tasks") or [])
    if isinstance(resp, list):
        return resp
    return []


class QuestNotConfigured(RuntimeError):
    #: A client with no URL or key sent nothing, so a write through it did not happen.
    write_status = ToolStatus.FAILED


class QuestApiError(RuntimeError):
    """A Quest API call failed.

    ``status`` is the HTTP status when Quest answered at all. ``retry_after`` is the seconds Quest asked the caller to
    wait (a 429 or 503). ``outcome_unknown`` is True when the request may have reached Quest but no answer came back
    (a read timeout, a reset after sending): for a write that means it MAY have been applied. ``transient`` marks a
    failure worth retrying (a transport error, 429 or 5xx); a 4xx other than 429 is a final answer."""

    def __init__(self, message: str, *, status: Optional[int] = None, retry_after: Optional[float] = None,
                 outcome_unknown: bool = False, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown
        self.transient = transient


class QuestWriteError(QuestApiError):
    """A write did not take effect, or may not have. ``write_status`` says which: ``refused`` (Quest said no; the
    message carries its reason), ``failed`` (it did not happen) or ``unknown`` (it was sent and not answered: it may
    have been applied, so look before trying again). Raised instead of returning an empty value."""

    def __init__(self, message: str, *, write_status: ToolStatus, status: Optional[int] = None,
                 retry_after: Optional[float] = None, outcome_unknown: bool = False, detail: str = ""):
        super().__init__(message, status=status, retry_after=retry_after, outcome_unknown=outcome_unknown)
        self.write_status = write_status
        self.detail = detail or message


class WriteResult:
    """What a write that took effect (or is waiting to) returns: ``status`` (applied, held or noop), ``id`` (the
    created or affected record, or the ask that holds it), ``detail`` (Quest's own words, if any) and ``data`` (the raw
    answer). It is never an empty value, and it reads like the answer it wraps (``get``, ``[]``, ``in``, ``==``), so a
    caller written against the plain payload keeps working. ``bool`` is True for applied, held and noop, False for
    the statuses a lifecycle report can carry without raising (refused, failed, unknown)."""

    ID_KEYS = ("id", "goal_id", "decision_id", "task_id", "note_id", "draft_id", "key")

    def __init__(self, data: Any, status: ToolStatus = ToolStatus.APPLIED, *, id: str = "", detail: str = ""):
        self.data = data
        self.status = status
        self.id = id
        self.detail = detail

    @classmethod
    def from_payload(cls, payload: Any, http_status: Optional[int] = None) -> "WriteResult":
        status = classify_payload(payload, http_status)
        detail, ident = "", ""
        if isinstance(payload, dict):
            detail = str(payload.get("reason") or payload.get("message") or "")
            held_id = decision_id_of(payload) if status is ToolStatus.HELD else None
            ident = str(held_id or next((payload[k] for k in cls.ID_KEYS if payload.get(k)), "") or "")
        return cls(payload, status, id=ident, detail=detail)

    @classmethod
    def not_done(cls, status: ToolStatus, detail: str) -> "WriteResult":
        return cls({}, status, detail=detail)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default) if isinstance(self.data, dict) else default

    def __getitem__(self, key: Any) -> Any:
        return self.data[key]

    def __contains__(self, key: Any) -> bool:
        return key in self.data if isinstance(self.data, (dict, list)) else False

    def __iter__(self):
        return iter(self.data if isinstance(self.data, (dict, list)) else ())

    def __len__(self) -> int:
        return len(self.data) if isinstance(self.data, (dict, list)) else 0

    def __bool__(self) -> bool:
        return self.status in DONE_STATUSES or self.status is ToolStatus.HELD

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, WriteResult):
            return (self.data, self.status) == (other.data, other.status)
        return self.data == other

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"WriteResult({self.status.value}, id={self.id!r}, detail={self.detail!r}, data={self.data!r})"


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date), else None. A date in the past is 0."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds) if seconds == seconds else None


class QuestPendingApproval(QuestApiError):
    """Quest did NOT create the quest: it filed an ask and is waiting for a person to approve it.

    A 2xx answer to ``POST /api/quests/start`` is not proof a quest exists. Callers must not report
    "created" or attach anything: there is no quest id yet. ``decision_id`` names the ask."""

    write_status = ToolStatus.HELD

    def __init__(self, message: str, *, decision_id: Optional[str] = None, status: Optional[int] = 202):
        super().__init__(message, status=status)
        self.decision_id = decision_id


# Quest's own validation for a decision's ``kind``: 1-64 chars, lowercase alnum/underscore/colon/
# hyphen (e.g. "approve", "explicit:spend", "notice:prod-ops"). Checked client-side so a bad kind
# degrades to "approve" instead of losing the whole escalation to a 422.
DECISION_KIND_RE = re.compile(r"^[a-z0-9_:-]{1,64}$")


# How many goals the ``list_quest_goal_updates`` FALLBACK may fan out over before it stops.
# The bulk route it prefers (``GET /api/planning/quests/{id}/goal-updates``) does not exist on
# every backend, and where it 404s this client falls back to one call PER GOAL. Spiritual Data's
# funding quest carries 216 goals, so that fallback fired 216 requests against a 60-per-minute
# limit and rate-limited the caller out of the API: on 2026-09-20 it took down the Sunday
# autopilot pass itself, whose own POST /api/assistant-tasks came back 429, so the brief Joshua
# reads was never created and never mailed. The fan-out is a best-effort convenience (goal
# check-ins are context, not the work), so it is bounded: read a sensible number of goals, say
# loudly in the log how many were left unread, and never spend a whole quest's rate-limit budget
# on it. The proper fix is the bulk route on the backend, which makes this whole path dead code.
GOAL_UPDATE_FANOUT_CAP = 40


# Env var naming the hours to add to "now" as a decision's deadline when the escalation itself
# specified none. Unset (the default) means no deadline is ever synthesized here.
QAR_DECISION_DEFAULT_DEADLINE_HOURS_ENV_VAR = "QAR_DECISION_DEFAULT_DEADLINE_HOURS"
