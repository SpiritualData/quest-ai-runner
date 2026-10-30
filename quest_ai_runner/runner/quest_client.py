"""QuestClient — the reference client for Quest's assistant-tasks + team-decisions API.

Generalized from the personal lane's stdlib-only ``personal_quest.py``. This is the integration
contract Quest was missing: a thin, dependency-light (urllib only) client to discover, claim,
run, and report queued AI tasks, plus raise team decision-requests for human-only steps.

Auth: a Quest API key ``qsk_<hex>`` carried as ``Authorization: Bearer qsk_...``. The key is the
team/executor identity (goal-linked tasks execute under the quest owner, so the owner's key
polls/updates them). NO key is baked in — it comes from config.

Endpoints implemented (the contract from integration_library_design.md §3):
  Discovery  : GET  /api/assistant-tasks?status=queued&due_before=<ISO-now>[&env_id=]
  Fast lane  : GET  /api/assistant-tasks/wait?real_time=true&timeout=<secs>[&team_id=&env_id=]
               (long-poll; blocks server-side until a real-time task is queued or timeout)
               GET  /api/assistant-tasks?status=queued&real_time=true  (fallback short poll)
  Claim      : PATCH /api/assistant-tasks/{id}  {status: in_progress}
  Report     : PATCH /api/assistant-tasks/{id}  {status: done|needs_you|failed, result, decision_id}
  Escalate   : POST  /api/teams/{team_id}/decisions  (a HOLD-default decision-request)
  Loop-close : GET  /api/teams/decisions/for-user, POST /api/teams/decisions/{id}/resolve
  Identity   : GET  /api/teams/whoami  (validate the key)
  AI-rep sync: GET/PUT /api/teams/{team_id}/members/{user_id}/ai-profile (all rep data),
               POST     /api/teams/{team_id}/members/{user_id}/corrections (one learned note)
  Account-wide quests (NOT team-scoped; a person's own goals — "goal is the hub"):
               GET  /api/quests/me                    (list_my_quests)
               GET  /api/quests/{quest_id}/state       (get_my_quest)
               GET  /api/quests/{quest_id}/notes       (list_quest_notes)
               POST /api/quests/{quest_id}/notes       (add_quest_note)
               GET  /api/quests/{quest_id}/events      (list_quest_events; quest lifecycle
               analytics events -- what the PERSON/AI/API changed, actor-attributed)
               GET  /api/quests/{quest_id}/context-entries            (list_context_entries)
               POST /api/quests/{quest_id}/context-entries            (create_context_entry)
               PUT  /api/quests/{quest_id}/context-entries/{entry_id} (update_context_entry)
               Notes are append-only (no PATCH/DELETE exists); context entries can be replaced in
               place, so a REFRESHING artifact belongs in an entry, not in a note.
  Goal (real, typed, period-scoped -- distinct from an assistant task):
               POST /api/planning/goals                (create_goal)
  Goal updates (the per-goal check-in thread -- what ``list_goal_notes`` falls back to, since the
               reference backend has no ``.../goals/{goal_id}/notes`` route):
               GET    /api/planning/goals/{goal_id}/updates          (list_goal_updates)
               POST   /api/planning/goals/{goal_id}/updates          (add_goal_update)
               DELETE /api/planning/goals/{goal_id}/updates/{upd_id} (delete_goal_update)
               GET    /api/planning/quests/{quest_id}/goal-updates   (list_quest_goal_updates;
               bulk path, falls back to a per-goal fan-out when the route is not yet available)
  Reflections (the person's own words; USER-scoped, no team or quest id):
               GET  /api/daily-plan/today              (get_daily_reflection)
               GET  /api/period-review/{period}/current (get_period_reflection)
               Composed into one context block by ``runner.reflections``.
  Insights (the person's own captures; USER-scoped, no team or quest id):
               GET   /api/data/insights/collection             (get_insights_collection)
               POST  /api/teams/{id}/members/{uid}/context-prefs (add_context_pref)
               GET   /api/data/collections                    (list_collections)
               GET   /api/data/collections/{id}/entries        (list_collection_entries)
               PATCH /api/data/insights/mark-acted-on          (mark_insight_acted_on)
               GET   /api/data/insights/by-quest              (list_quest_insights; the latest
               insights ASSIGNED to one quest, newest first, unacted by default. An older backend
               without the route answers 404, which reads as no insights.)
               The generic entries route has NO server-side date or field filter, and context
               assembly no longer walks it: ``runner.insights`` reads one quest's insights only.

``QuestDecisionSink`` wraps a QuestClient as a core ``EscalationSink`` so the brain can raise a
confirm/decision and get back a ``decision_id`` to report on the task.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence

from esoi import implementation
from ..core.adapters_values import Escalation
from ..core.adapters_bases import EscalationSinkBase

from .quest_client_support import (
    QAR_DECISION_DEFAULT_DEADLINE_HOURS_ENV_VAR,
    QuestApiError,
    QuestNotConfigured,
    QuestPendingApproval,
    QuestWriteError,
    WriteResult,
    _PERIOD_RE,
    log,
)
from .quest_client_transport import TransportMixin
from .quest_client_tasks import TasksMixin
from .quest_client_decisions import DecisionsMixin
from .quest_client_goals import GoalsMixin
from .quest_client_quests import QuestsMixin
from .quest_client_context import ContextEntriesMixin
from .quest_client_reps import RepsMixin
from .quest_client_intents import IntentsMixin




class QuestClient(TransportMixin, TasksMixin, DecisionsMixin, GoalsMixin, QuestsMixin, ContextEntriesMixin, RepsMixin,
                  IntentsMixin):
    """Thin urllib client for the Quest task + decision API. No third-party deps.

    The contract every method follows is in ``docs/quest-api-contract.md`` ("Client contract"): a content write returns a
    ``WriteResult`` or raises a ``QuestWriteError`` and never returns an empty value; the lifecycle reports return a
    falsy ``WriteResult`` instead of raising; reads where absence is normal return an empty value."""

    def __init__(self, base_url: str, api_key: str, *, team_id: Optional[str] = None,
                 timeout: Optional[float] = None,
                 decision_assignees: Optional[Dict[str, str]] = None,
                 default_assignee_user_id: Optional[str] = None,
                 env_id: Optional[str] = None,
                 read_retries: Optional[int] = None,
                 write_retry: Optional[str] = None,
                 retry_max_delay: Optional[float] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.team_id = team_id or ""
        # Which of the team's environments this runner is (None = the default env). Used to read that
        # environment's work-model configuration when a task does not name its own env_id.
        self.env_id = env_id or ""
        # Timeout, retry and write-retry settings: an argument wins, else QAR_QUEST_* from the environment.
        self.init_transport(timeout=timeout, read_retries=read_retries, write_retry=write_retry,
                            retry_max_delay=retry_max_delay)
        # {role name -> user id} for ``create_decision(assignee="...")``. WHO a decision goes to is
        # deployment policy, not client logic: one deployment routes money to an owner and errands
        # to an operator, another has a single approver. Naming the roles in config (RunnerConfig.
        # decision_assignees) keeps that policy out of every call site and out of this library.
        self.decision_assignees: Dict[str, str] = dict(decision_assignees or {})
        self.default_assignee_user_id = default_assignee_user_id or ""
        # quest_id -> the team that actually owns it, resolved lazily by
        # owning_team_for and only when the configured team turns out not to. An entry is
        # dropped the moment the team it names fails to serve the quest, so a quest moved a
        # second time is re-resolved instead of 404ing until the process restarts.
        self._quest_team_cache: Dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _require(self):
        if not self.configured:
            raise QuestNotConfigured(
                "Quest base URL and API key (qsk_...) are required. Supply them via RunnerConfig.")

    # --- identity ------------------------------------------------------------

    def whoami(self) -> Dict[str, Any]:
        """Validate the key and return the authenticated executor identity."""
        try:
            return self._request("GET", "/api/teams/whoami") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("whoami failed: %s", e)
            return {}

    # --- discovery -----------------------------------------------------------

    def team_param(self, team_id: Optional[str] = None,
                   team_ids: Optional[Sequence[str]] = None) -> Optional[str]:
        """The value to send as the ``team_id`` query param, for the discovery calls below.

        ``team_ids`` non-empty: the comma-joined set, which the backend's assistant-task listing
        accepts as "any of these teams". ONE request covers the whole set.

        Otherwise: exactly the pre-existing fallback -- the caller's ``team_id`` when they named
        one (including ``""`` for owner-scoped), else the client's own configured team.

        THE GUARANTEE THIS EXISTS FOR: a single-element ``team_ids`` produces the identical string
        a bare ``team_id`` of that same id produces (``",".join(["t"]) == "t"``), and an empty
        ``team_ids`` changes nothing at all. So a single-team lane's request is byte-identical to
        what it sent before multi-team discovery existed, which is what makes this addition a
        provable no-op for every existing deployment.
        """
        if team_ids:
            return ",".join(team_ids)
        return self.team_id if team_id is None else team_id

    # --- the usage-limit wait (see core.usage_limit) --------------------------

    @staticmethod
    def utc_stamp(moment: datetime) -> str:
        """An aware datetime as the backend's start_at form: UTC, second precision, trailing Z."""
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def error_detail(error: QuestApiError) -> str:
        """The backend's own ``detail`` text from a ``QuestApiError`` raised by ``_request``
        (message shape ``Quest API POST /path -> 400: {"detail": "..."}``), else the body as sent."""
        text = str(error)
        body = text.split(" -> ", 1)[1].partition(": ")[2] if " -> " in text else text
        try:
            payload = json.loads(body)
            detail = payload.get("detail")
        except (ValueError, AttributeError):
            return body
        if isinstance(detail, str):
            return detail
        if detail is None and isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"]
        return json.dumps(detail) if detail is not None else body


@implementation("escalate-to-a-human", "quest_decision")
class QuestDecisionSink(EscalationSinkBase):
    """Adapt a QuestClient into a core EscalationSink.

    The brain calls ``escalate(Escalation)`` for a human-only step; this creates a HOLD-default
    team decision-request and returns its ``decision_id`` (which the executor stamps on the task
    via ``report_needs_you``).
    """

    def __init__(self, client: QuestClient, *, default_assignee_user_id: Optional[str] = None,
                default_deadline_hours: Optional[float] = None):
        self._client = client
        self._default_assignee = default_assignee_user_id
        self.default_deadline_hours = default_deadline_hours

    def quest_owner_assignee(self, quest_id: Optional[str]) -> str:
        """The quest's owner user id, as a last-resort escalation assignee, or "".

        Best-effort: no ``quest_id``, an unreadable quest, or a quest with no recorded owner all
        return "" -- the caller's own "never file an unaddressed decision" rule is what actually
        fires on that, this lookup never pretends to know something it does not.
        """
        if not quest_id:
            return ""
        try:
            quest = self._client.get_quest(quest_id)
        except Exception:  # noqa: BLE001 -- a failed fallback lookup must not break escalation
            return ""
        owners = (quest or {}).get("owner_user_ids") or []
        return str(owners[0]) if owners else ""

    def resolve_assignee(self, escalation: Escalation) -> str:
        """WHO this decision goes to: explicit escalation assignee, else the configured default,
        else the quest's own owner.

        Raises ``QuestNotConfigured`` instead of returning "" so ``escalate`` below never files a
        decision with an empty ``assigned_to_user_id`` -- that used to succeed silently and sit in
        nobody's queue forever, which is a worse failure than the escalation not landing at all
        (at least a raised, logged failure is visible in the run's own error trail).
        """
        assignee = (escalation.assignee or self._default_assignee
                   or self.quest_owner_assignee(escalation.quest_id))
        if assignee:
            return assignee
        raise QuestNotConfigured(
            f"cannot raise a decision ({escalation.summary!r}) for quest "
            f"{escalation.quest_id!r}: no explicit assignee, no configured default "
            "(QAR_DECISION_ASSIGNEE), and the quest's owner could not be resolved. Refusing to "
            "file an unaddressed decision.")

    def resolve_deadline(self, escalation: Escalation) -> Optional[datetime]:
        """The deadline to send: the escalation's own, else this sink's configured default hours.

        The default hours come from ``default_deadline_hours`` (constructor) or, absent that, the
        ``QAR_DECISION_DEFAULT_DEADLINE_HOURS`` env var -- read here, at call time, rather than
        only once at construction, so a consumer that sets just the env var (no RunnerConfig
        wiring) still gets it. Unset either way means no deadline is added, exactly as before this
        capability existed.
        """
        if escalation.deadline is not None:
            return escalation.deadline
        hours = self.default_deadline_hours
        if hours is None:
            raw = os.getenv(QAR_DECISION_DEFAULT_DEADLINE_HOURS_ENV_VAR)
            if raw:
                try:
                    hours = float(raw)
                except ValueError:
                    log.warning("%s=%r is not a valid number; no default deadline applied",
                               QAR_DECISION_DEFAULT_DEADLINE_HOURS_ENV_VAR, raw)
        if not hours:
            return None
        return datetime.now(timezone.utc) + timedelta(hours=hours)

    def open_decision_ids_for_quest(self, quest_id: str) -> FrozenSet[str]:
        """Concrete implementation of the optional ``EscalationSinkBase`` recovery capability.

        Best-effort: [] / any failure -> empty set, so a caller diffing before/after snapshots
        never mistakes "could not check" for "confirmed no new decision."
        """
        try:
            rows = self._client.list_open_decisions_for_quest(quest_id) or []
        except Exception:  # noqa: BLE001 -- a failed probe must never break the run
            return frozenset()
        return frozenset(
            str(d.get("decision_id") or d.get("id"))
            for d in rows if d.get("decision_id") or d.get("id")
        )

    @staticmethod
    def _same_ask(a: str, b: str) -> bool:
        """Whether two decision summaries are the same ask, ignoring incidental formatting."""
        norm = lambda s: " ".join(str(s or "").split()).strip().lower()  # noqa: E731
        return bool(norm(a)) and norm(a) == norm(b)

    def _existing_open_decision(self, escalation: Escalation) -> str:
        """The id of an OPEN decision already asking this, or "".

        A run that needs the same answer every morning would otherwise file a new request every
        morning: the person opens their queue to five rows for one question they have already seen,
        and resolving it clears only today's. Asking again in what they READ is right -- somebody
        who has not got to it needs reminding -- but the record should stay one row.
        """
        if not escalation.quest_id:
            return ""
        try:
            for decision in self._client.list_open_decisions_for_quest(escalation.quest_id) or []:
                if self._same_ask(escalation.summary, decision.get("summary")):
                    return str(decision.get("decision_id") or decision.get("id") or "")
        except Exception:  # noqa: BLE001 — a dedupe lookup must never block an escalation
            return ""
        return ""

    def team_for(self, quest_id: Optional[str]) -> Optional[str]:
        """The team a decision about ``quest_id`` belongs on, or None for the client's own team.

        A lane that discovers work from several teams escalates about quests on any of them, and
        ``create_decision`` defaults to the CLIENT's home team. Filing team1's question on team2
        puts it in front of the wrong people (or nobody), so resolve the quest's own team first.
        Cheap and safe: ``owning_team_for`` is cached per quest and answers "" when it cannot
        tell, in which case the old default stands. Never raises -- an escalation must reach a
        human even if the lookup cannot.
        """
        if not quest_id:
            return None
        try:
            return self._client.owning_team_for(quest_id) or None
        except Exception:  # noqa: BLE001 -- routing is a refinement; never lose the escalation
            log.info("escalate: could not resolve the owning team for quest %s", quest_id,
                     exc_info=True)
            return None

    def escalate(self, escalation: Escalation) -> str:
        existing = self._existing_open_decision(escalation)
        if existing:
            log.info("escalate: reusing open decision %s rather than filing a duplicate", existing)
            return existing
        try:
            assignee_user_id = self.resolve_assignee(escalation)
            res = self._client.create_decision(
                escalation.summary,
                kind=escalation.kind,
                quest_id=escalation.quest_id,
                team_id=self.team_for(escalation.quest_id),
                assignee_user_id=assignee_user_id,
                default_on_silence=escalation.default_on_silence,
                deadline=self.resolve_deadline(escalation),
                review_url=escalation.review_url,
            )
            # The API returns the created decision; surface its id (best-effort across field names).
            return str((res or {}).get("decision_id") or (res or {}).get("id") or "")
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("escalate failed: %s", e)
            return ""


def quest_client_from_env(env: Mapping[str, str]) -> QuestClient:
    """A ``QuestClient`` from ``QUEST_BASE_URL`` (or ``QUEST_API_URL``), ``QUEST_API_KEY`` and ``QUEST_TEAM_ID``.

    This is the factory the standard tools in ``core.tools`` build their client with: ``core`` may
    not import the runner, so ``quest_ai_runner/__init__.py`` registers it with
    ``core.tools.set_quest_client_factory``.
    """
    base = env.get("QUEST_BASE_URL") or env.get("QUEST_API_URL") or ""
    return QuestClient(base_url=base, api_key=env.get("QUEST_API_KEY") or "",
                       team_id=env.get("QUEST_TEAM_ID") or None)
