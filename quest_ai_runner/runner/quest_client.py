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

``QuestDecisionSink`` wraps a QuestClient as a core ``EscalationSink`` so the brain can raise a
confirm/decision and get back a ``decision_id`` to report on the task.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.adapters import Escalation, EscalationSinkBase


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

    def claim(self, task_id: str) -> Dict[str, Any]:
        """PATCH -> in_progress (backend-aware dedup: a claimed task won't re-fire)."""
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "in_progress"})

    def report_done(self, task_id: str, result: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "done", "result": result})

    def report_needs_you(self, task_id: str, result: str, decision_id: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "needs_you", "result": result, "decision_id": decision_id})

    def report_failed(self, task_id: str, result: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                             body={"status": "failed", "result": result})

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
                                   team_id: Optional[str] = None) -> Dict[str, Any]:
        """Tell the backend this runner is ALIVE and what it can do (the env heartbeat).

        POSTs to ``/api/teams/{team_id}/environment/heartbeat`` with the runner's declared
        ``capabilities`` ({web, corpus, code}). The backend stamps last_heartbeat_at, stores the
        reported capabilities, and auto-registers the team's env config on first contact — making
        the env queryable by the routing classifier. Authed by THIS runner's qsk_ key (its user
        must be a member of the team). Returns the stored env state.

        ``team_id`` defaults to the client's configured team. The CALLER (poller) keeps this
        best-effort: a failed heartbeat must never break task execution.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to post an environment heartbeat")
        body: Dict[str, Any] = {"capabilities": dict(capabilities)}
        if runner_label:
            body["runner_label"] = runner_label
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
