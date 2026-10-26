"""Quest client support: logger, the API error types, shared patterns and limits.

Split out of ``quest_client.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional


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
    pass


class QuestApiError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class QuestPendingApproval(QuestApiError):
    """Quest did NOT create the quest: it filed an ask and is waiting for a person to approve it.

    A 2xx answer to ``POST /api/quests/start`` is not proof a quest exists. Callers must not report
    "created" or attach anything: there is no quest id yet. ``decision_id`` names the ask."""

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
