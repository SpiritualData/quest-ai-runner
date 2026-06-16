"""QuestClient — the reference client for Quest's assistant-tasks + team-decisions API.

Generalized from the personal lane's stdlib-only ``personal_quest.py``. This is the integration
contract Quest was missing: a thin, dependency-light (urllib only) client to discover, claim,
run, and report queued AI tasks, plus raise team decision-requests for human-only steps.

Auth: a Quest API key ``qsk_<hex>`` carried as ``Authorization: Bearer qsk_...``. The key is the
team/executor identity (goal-linked tasks execute under the quest owner, so the owner's key
polls/updates them). NO key is baked in — it comes from config.

Endpoints implemented (the contract from integration_library_design.md §3):
  Discovery  : GET  /api/assistant-tasks?status=queued&due_before=<ISO-now>
  Claim      : PATCH /api/assistant-tasks/{id}  {status: in_progress}
  Report     : PATCH /api/assistant-tasks/{id}  {status: done|needs_you|failed, result, decision_id}
  Escalate   : POST  /api/teams/{team_id}/decisions  (a HOLD-default decision-request)
  Loop-close : GET  /api/teams/decisions/for-user, POST /api/teams/decisions/{id}/resolve
  Identity   : GET  /api/teams/whoami  (validate the key)
  AI-rep sync: GET/PUT /api/teams/{team_id}/members/{user_id}/ai-profile (all rep data),
               POST     /api/teams/{team_id}/members/{user_id}/corrections (one learned note)

``QuestDecisionSink`` wraps a QuestClient as a core ``EscalationSink`` so the brain can raise a
confirm/decision and get back a ``decision_id`` to report on the task.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.adapters import Escalation, EscalationSinkBase

log = logging.getLogger("quest-ai-runner.quest_client")


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


class QuestClient:
    """Thin urllib client for the Quest task + decision API. No third-party deps."""

    def __init__(self, base_url: str, api_key: str, *, team_id: Optional[str] = None,
                 timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.team_id = team_id or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _require(self):
        if not self.configured:
            raise QuestNotConfigured(
                "Quest base URL and API key (qsk_...) are required. Supply them via RunnerConfig.")

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        self._require()
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise QuestApiError(f"Quest API {method} {path} -> {e.code}: {detail}", status=e.code) from e
        except urllib.error.URLError as e:
            raise QuestApiError(f"Quest API {method} {path} unreachable: {e.reason}") from e

    # --- identity ------------------------------------------------------------

    def whoami(self) -> Dict[str, Any]:
        """Validate the key and return the authenticated executor identity."""
        return self._request("GET", "/api/teams/whoami") or {}

    # --- discovery -----------------------------------------------------------

    def discover_due(self, *, now: Optional[datetime] = None,
                     status: str = "queued",
                     team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET queued tasks due at/before now (the poll-mode discovery floor).

        Unscheduled tasks count as 'due now'; scheduled ones surface within their window.

        ``team_id`` scopes discovery to ONE team's tasks (per-team lane isolation): two teams
        under the SAME owner share one owner-scoped queue, so without this filter each lane would
        discover BOTH teams' tasks and race to claim/run them with the wrong corpus/escalation.
        Defaults to the client's configured ``team_id`` so a team-bound lane is isolated by default;
        pass ``team_id=""`` (or configure none) for an owner-scoped, teamless discovery. The backend
        treats a task's null ``team_id`` as owner-scoped, so a team filter only narrows, never breaks
        the personal lane.

        The Quest list endpoint returns an envelope ``{"tasks": [...], "count": N}`` (not a
        bare array), so we unwrap ``tasks`` here; a bare-list response is also tolerated so the
        client works against a mock or a future shape change.
        """
        now = now or datetime.now(timezone.utc)
        iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tid = self.team_id if team_id is None else team_id
        params: Dict[str, Any] = {"status": status, "due_before": iso}
        if tid:
            params["team_id"] = tid
        resp = self._request("GET", "/api/assistant-tasks", params=params)
        return _as_task_list(resp)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/assistant-tasks/{task_id}") or {}

    # --- claim / report ------------------------------------------------------

    def claim(self, task_id: str, handler: Optional[str] = None) -> Dict[str, Any]:
        """PATCH -> in_progress (backend-aware dedup: a claimed task won't re-fire).

        When ``handler`` is given (the slug of the AI representation/skill that will run this task,
        e.g. ``"joshua"`` / ``"subham"``, or a runner label), it is stamped on the task so the
        Quest task-detail modal can show "handled by X". Omit it and the claim body is unchanged
        (fully backward compatible)."""
        body: Dict[str, Any] = {"status": "in_progress"}
        if handler:
            body["handler"] = handler
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)

    def report_done(self, task_id: str, result: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "done", "result": result})

    def report_needs_you(self, task_id: str, result: str, decision_id: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "needs_you", "result": result, "decision_id": decision_id})

    def report_failed(self, task_id: str, result: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "failed", "result": result})

    # --- live execution progress onto the task (the task-detail stream) ------

    def report_progress(self, task_id: str, kind: str, *, text: Optional[str] = None,
                        output: Optional[str] = None,
                        data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST a live execution-progress event onto a task, for the task-detail modal's stream.

        ``kind`` is one of started|status|exec|output|done|error. Only the non-None fields are
        sent (text/output/data), mirroring how ``post_conversation_message`` builds its body.

        BEST-EFFORT BY CONTRACT: a dropped progress post must never fail the task, so this never
        raises — any API/transport error is logged as a warning and an empty dict returned. (The
        terminal result still goes via ``report_done`` / ``report_failed``, not this stream.)
        """
        body: Dict[str, Any] = {"kind": kind}
        if text is not None:
            body["text"] = text
        if output is not None:
            body["output"] = output
        if data is not None:
            body["data"] = data
        try:
            return self._request(
                "POST", f"/api/assistant-tasks/{task_id}/progress", body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("progress post for task %s (%s) failed: %s", task_id, kind, e)
            return {}

    # --- live progress into the originating chat -----------------------------

    def post_conversation_message(self, conv_id: str, content: str, *,
                                  kind: str = "progress") -> Dict[str, Any]:
        """Append a LIVE progress message into the Quest AI conversation a task came from.

        This is how a chat-delegated background task keeps the chat from going silent: the runner
        posts ``started`` when it picks the task up, ``progress`` for real milestones, and ``done``
        with the result. It executes under the quest OWNER's identity (the ``qsk_`` key), and the
        endpoint is owner-scoped, so the runner is simply appending to the owner's own conversation.

        Best-effort by contract at the call site (a dropped progress post must never fail the task),
        but this method itself surfaces API errors so callers can log them.
        """
        return self._request("POST", f"/api/quest-ai/conversations/{conv_id}/progress",
                             body={"content": content, "kind": kind}) or {}

    # --- environment heartbeat (so the backend knows this runner is live) ----

    def post_environment_heartbeat(self, capabilities: Dict[str, bool], *,
                                   runner_label: Optional[str] = None,
                                   env_id: Optional[str] = None,
                                   team_id: Optional[str] = None) -> Dict[str, Any]:
        """Tell the backend this runner is ALIVE and what it can do (the env heartbeat).

        POSTs to ``/api/teams/{team_id}/environment/heartbeat`` with the runner's declared
        ``capabilities`` ({web, corpus, code}). The backend stamps last_heartbeat_at, stores the
        reported capabilities, and auto-registers the team's env config on first contact — making
        the env queryable by the routing classifier. Authed by THIS runner's qsk_ key (its user
        must be a member of the team). Returns the stored env state.

        ``env_id`` identifies WHICH of the team's environments this runner is — a team can attach
        several runners, each its own environment. Omit it and this runner is the team's DEFAULT
        environment (so a single-runner deployment needs no extra config). ``team_id`` defaults to
        the client's configured team. The CALLER (poller) keeps this best-effort: a failed
        heartbeat must never break task execution.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to post an environment heartbeat")
        body: Dict[str, Any] = {"capabilities": dict(capabilities)}
        if runner_label:
            body["runner_label"] = runner_label
        if env_id:
            body["env_id"] = env_id
        return self._request("POST", f"/api/teams/{tid}/environment/heartbeat", body=body) or {}

    # --- escalation (team decision-requests; the confirm-before-act surface) --

    def create_decision(self, summary: str, *, kind: str = "approve",
                        quest_id: Optional[str] = None, assignee_user_id: Optional[str] = None,
                        default_on_silence: str = "hold",
                        team_id: Optional[str] = None) -> Dict[str, Any]:
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to raise a decision-request")
        body: Dict[str, Any] = {"kind": kind, "summary": summary,
                                "default_on_silence": default_on_silence}
        if quest_id:
            body["quest_id"] = quest_id
        if assignee_user_id:
            body["assigned_to_user_id"] = assignee_user_id
        return self._request("POST", f"/api/teams/{tid}/decisions", body=body)

    def list_open_decisions_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/teams/decisions/for-user",
                             params={"user_id": user_id}) or []

    def resolve_decision(self, decision_id: str, resolution: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/teams/decisions/{decision_id}/resolve",
                             body={"resolution": resolution})

    # --- quest and goal browsing (for interactive chat context selection) ------

    def list_quests(self, *, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/quests — all quests attached to the team.

        Returns a list of dicts with keys: quest_id, outcome, completed, owner_user_ids.
        Requires team_id either here or on the client instance.
        """
        self._require()
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to list quests")
        resp = self._request("GET", f"/api/teams/{tid}/quests") or []
        return resp if isinstance(resp, list) else []

    def list_quest_goals(self, quest_id: str, *,
                         team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals — goals grouped by time period.

        Returns {quest_id, outcome, period_groups: [{time_scope, period, period_label, goals: [{id,
        name, time_scope, period, period_label, deadline, completed, parent_goal_id}]}]}.
        Groups are ordered year → quarter → month → week → day → custom, then chronologically.
        Requires team_id either here or on the client instance.
        """
        self._require()
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to list quest goals")
        return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}/goals") or {}

    # --- task creation (enqueue a new AI task) --------------------------------

    def create_task(self, text: str, *,
                    team_id: Optional[str] = None,
                    goal_id: Optional[str] = None,
                    scheduled_at: Optional[str] = None,
                    source: str = "cli") -> Dict[str, Any]:
        """POST a new queued AI task to /api/assistant-tasks.

        ``team_id`` routes the task to a specific team's runner (defaults to the client's
        configured team). ``goal_id`` attaches the task to a goal so results appear on it.
        ``scheduled_at`` is an ISO-8601 UTC datetime string; omit it to run as soon as the
        runner's next poll picks it up. Returns the created task dict (includes its ``id``).
        """
        body: Dict[str, Any] = {"text": text, "source": source}
        tid = team_id if team_id is not None else self.team_id
        if tid:
            body["team_id"] = tid
        if goal_id is not None:
            body["goal_id"] = goal_id
        if scheduled_at is not None:
            body["scheduled_at"] = scheduled_at
        return self._request("POST", "/api/assistant-tasks", body=body) or {}

    # --- AI-rep profile (the rep <-> skill-file sync surface) -----------------

    def get_ai_profile(self, user_id: str, *, team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET ALL data for a team member's AI rep.

        Returns ``{user_id, display_name, persona, learned_notes: [{id, text, created_at,
        source, message_id?}], updated_at}``. This is the single source of truth the runner
        renders into the rep's local Claude skill file(s). ``team_id`` defaults to the client's
        configured team.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to read an AI-rep profile")
        return self._request("GET", f"/api/teams/{tid}/members/{user_id}/ai-profile") or {}

    def update_ai_profile(self, user_id: str, *, display_name: Optional[str] = None,
                          persona: Optional[str] = None,
                          learned_notes: Optional[List[Dict[str, Any]]] = None,
                          team_id: Optional[str] = None) -> Dict[str, Any]:
        """PUT edits to a rep's profile (any subset of display_name / persona / learned_notes).

        Only the fields you pass are sent, so a local edit to just the persona pushes up only the
        persona. Returns the updated profile. ``team_id`` defaults to the client's configured team.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to update an AI-rep profile")
        body: Dict[str, Any] = {}
        if display_name is not None:
            body["display_name"] = display_name
        if persona is not None:
            body["persona"] = persona
        if learned_notes is not None:
            body["learned_notes"] = learned_notes
        return self._request(
            "PUT", f"/api/teams/{tid}/members/{user_id}/ai-profile", body=body) or {}

    def add_rep_correction(self, user_id: str, correction: str, *,
                           message_id: Optional[str] = None,
                           team_id: Optional[str] = None) -> Dict[str, Any]:
        """POST a single correction (a learned note) for a rep; returns the updated learned_notes.

        Use this for the incremental "the chat just corrected the rep" path; use
        ``update_ai_profile`` to replace the whole notes list. ``team_id`` defaults to the
        client's configured team.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to add a rep correction")
        body: Dict[str, Any] = {"correction": correction}
        if message_id is not None:
            body["message_id"] = message_id
        return self._request(
            "POST", f"/api/teams/{tid}/members/{user_id}/corrections", body=body) or {}


class QuestDecisionSink(EscalationSinkBase):
    """Adapt a QuestClient into a core EscalationSink.

    The brain calls ``escalate(Escalation)`` for a human-only step; this creates a HOLD-default
    team decision-request and returns its ``decision_id`` (which the executor stamps on the task
    via ``report_needs_you``).
    """

    def __init__(self, client: QuestClient, *, default_assignee_user_id: Optional[str] = None):
        self._client = client
        self._default_assignee = default_assignee_user_id

    def escalate(self, escalation: Escalation) -> str:
        res = self._client.create_decision(
            escalation.summary,
            kind=escalation.kind,
            quest_id=escalation.quest_id,
            assignee_user_id=escalation.assignee or self._default_assignee,
            default_on_silence=escalation.default_on_silence,
        )
        # The API returns the created decision; surface its id (best-effort across field names).
        return str((res or {}).get("decision_id") or (res or {}).get("id") or "")
