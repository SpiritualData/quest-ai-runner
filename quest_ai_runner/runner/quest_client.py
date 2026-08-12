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
  Goal (real, typed, period-scoped -- distinct from an assistant task):
               POST /api/planning/goals                (create_goal)

``QuestDecisionSink`` wraps a QuestClient as a core ``EscalationSink`` so the brain can raise a
confirm/decision and get back a ``decision_id`` to report on the task.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.adapters import Escalation, EscalationSinkBase

log = logging.getLogger("quest-ai-runner.quest_client")

# Period formats accepted by quest-backend's app/utils/period_utils (day/week/month/quarter/year).
_PERIOD_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{4}_W\d{2}|\d{4}_\d{2}|\d{4}_Q[1-4]|\d{4})$"
)


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
                 body: Optional[Dict[str, Any]] = None,
                 timeout_override: Optional[float] = None) -> Any:
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
        # timeout_override lets a caller ask for a LONGER socket timeout than the client default
        # (e.g. the long-poll wait channel, which asks the server to hold the request for up to
        # ~25-30s and must not have its own transport cut that short).
        try:
            with urllib.request.urlopen(req, timeout=(timeout_override or self.timeout)) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise QuestApiError(f"Quest API {method} {path} -> {e.code}: {detail}", status=e.code) from e
        except urllib.error.URLError as e:
            raise QuestApiError(f"Quest API {method} {path} unreachable: {e.reason}") from e
        except (TimeoutError, OSError) as e:
            # A socket-level timeout/reset during the read escapes urlopen as a raw TimeoutError/
            # OSError, NOT a URLError — without this wrap it would blow through every "never
            # raises" caller contract (e.g. wait_for_interactive) and spam the poller with
            # tracebacks instead of a calm retry.
            raise QuestApiError(f"Quest API {method} {path} transport error: {e}") from e

    # --- identity ------------------------------------------------------------

    def whoami(self) -> Dict[str, Any]:
        """Validate the key and return the authenticated executor identity."""
        try:
            return self._request("GET", "/api/teams/whoami") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("whoami failed: %s", e)
            return {}

    # --- discovery -----------------------------------------------------------

    def discover_due(self, *, now: Optional[datetime] = None,
                     status: str = "queued",
                     team_id: Optional[str] = None,
                     env_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET queued tasks due at/before now (the poll-mode discovery floor).

        Unscheduled tasks count as 'due now'; scheduled ones surface within their window.

        ``team_id`` scopes discovery to ONE team's tasks (per-team lane isolation): two teams
        under the SAME owner share one owner-scoped queue, so without this filter each lane would
        discover BOTH teams' tasks and race to claim/run them with the wrong corpus/escalation.
        Defaults to the client's configured ``team_id`` so a team-bound lane is isolated by default;
        pass ``team_id=""`` (or configure none) for an owner-scoped, teamless discovery. The backend
        treats a task's null ``team_id`` as owner-scoped, so a team filter only narrows, never breaks
        the personal lane.

        ``env_id`` scopes discovery further to ONE of the team's runner ENVIRONMENTS: a multi-env
        team routes a task pinned to a specific runner via ``env_id``, and the backend's list
        endpoint matches that env PLUS any unpinned (``env_id=None``) task, so passing this only
        narrows -- it never strands general work. Omit it (the default) for the pre-multi-env
        contract (every runner sees every one of the team's unpinned + all-env tasks).

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
        if env_id:
            params["env_id"] = env_id
        try:
            resp = self._request("GET", "/api/assistant-tasks", params=params)
            return _as_task_list(resp)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("discover_due failed: %s", e)
            return []

    def list_interactive_due(self, *, team_id: Optional[str] = None,
                             env_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET queued REAL-TIME tasks only (the fast-lane FALLBACK poll).

        Real-time tasks are ones a task-creation path flagged as low-latency-eligible (a live chat
        context-request and similar) because a caller is waiting on a reply right now -- see
        ``wait_for_interactive`` for the preferred, lower-latency long-poll channel. This method is
        the fallback used when the wait channel is disabled (``QAR_WAIT_CHANNEL=0``): the fast lane
        calls it on a short interval (``QAR_CONTEXT_POLL_SECONDS``) instead of waiting out the full
        background ``poll_interval_seconds``. Same team/env scoping as ``discover_due``. Never raises.
        """
        tid = self.team_id if team_id is None else team_id
        params: Dict[str, Any] = {"status": "queued", "real_time": "true"}
        if tid:
            params["team_id"] = tid
        if env_id:
            params["env_id"] = env_id
        try:
            resp = self._request("GET", "/api/assistant-tasks", params=params)
            return _as_task_list(resp)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_interactive_due failed: %s", e)
            return []

    def wait_for_interactive(self, *, team_id: Optional[str] = None,
                             env_id: Optional[str] = None,
                             timeout: float = 25.0) -> Optional[Dict[str, Any]]:
        """Long-poll GET /api/assistant-tasks/wait -- the presence-aware PUSH channel.

        Blocks SERVER-SIDE (the backend holds the connection, polling Mongo internally) until a
        real-time task is queued for this team/env, or ``timeout`` elapses with nothing to
        deliver. The runner is meant to call this in a tight loop from its own thread: reconnect
        immediately after each return (empty or not) so a live chat context-request is answered in
        close to real time whenever this lane is up, without the fixed-poll latency of
        ``poll_interval_seconds`` (default 900s).

        Returns the queued task dict, or ``None`` on an empty/timed-out wait (the ordinary, expected
        outcome most of the time) or on ANY transport error (unconfigured client, network failure,
        endpoint not yet available on an older backend). Never raises. The socket-level timeout is
        padded past the server's own bound so a legitimate near-``timeout`` wait is never cut short
        by our own transport.
        """
        tid = self.team_id if team_id is None else team_id
        params: Dict[str, Any] = {"real_time": "true", "timeout": timeout}
        if tid:
            params["team_id"] = tid
        if env_id:
            params["env_id"] = env_id
        try:
            resp = self._request(
                "GET", "/api/assistant-tasks/wait", params=params,
                timeout_override=timeout + 10.0,
            )
            task = (resp or {}).get("task")
            return task or None
        except (QuestApiError, QuestNotConfigured) as e:
            log.info("wait_for_interactive unavailable (%s) -- the fast lane will retry", e)
            return None

    def get_task(self, task_id: str) -> Dict[str, Any]:
        try:
            return self._request("GET", f"/api/assistant-tasks/{task_id}") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_task failed for %s: %s", task_id, e)
            return {}

    def is_task_cancelled(self, task_id: str) -> bool:
        """True when the task has been cancelled mid-execution (a human hit "stop").

        Re-fetches the task via ``get_task`` and reports True when its ``status`` is
        ``"cancelled"`` or its ``cancel_requested`` field is truthy (the backend sets both when a
        background task is cancelled, but a caller only needs one signal). FAIL-OPEN by contract:
        any error (network, unconfigured client, missing task) returns False rather than raising, so
        a transient API hiccup can never be mistaken for a cancellation and kill a legitimate run.
        """
        try:
            task = self.get_task(task_id) or {}
            if str(task.get("status") or "").strip().lower() == "cancelled":
                return True
            return bool(task.get("cancel_requested"))
        except Exception:  # noqa: BLE001 -- fail-open: never let this check kill a real run
            log.warning("is_task_cancelled check failed for %s", task_id, exc_info=True)
            return False

    # --- claim / report ------------------------------------------------------

    def claim(self, task_id: str, handler: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """PATCH -> in_progress (backend-aware dedup: a claimed task won't re-fire).

        When ``handler`` is given (the slug of the AI representation/skill that will run this task,
        e.g. ``"alex"`` / ``"sam"``, or a runner label), it is stamped on the task so the
        Quest task-detail modal can show "handled by X". Omit it and the claim body is unchanged
        (fully backward compatible).

        Returns the PATCHed task dict on success, or ``None`` on failure (API error or unconfigured
        client) — the caller MUST treat ``None`` as "not claimed" and not proceed to execute the
        task, since an empty dict would be indistinguishable from a successful-but-empty response.
        """
        body: Dict[str, Any] = {"status": "in_progress"}
        if handler:
            body["handler"] = handler
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("claim failed for task %s: %s", task_id, e)
            return None

    def report_done(self, task_id: str, result: str) -> Dict[str, Any]:
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body={"status": "done", "result": result})
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_done failed for task %s: %s", task_id, e)
            return {}

    def report_needs_you(self, task_id: str, result: str, decision_id: str) -> Dict[str, Any]:
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body={"status": "needs_you", "result": result, "decision_id": decision_id})
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_needs_you failed for task %s: %s", task_id, e)
            return {}

    def report_failed(self, task_id: str, result: str) -> Dict[str, Any]:
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body={"status": "failed", "result": result})
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_failed for task %s: %s", task_id, e)
            return {}

    def report_done_with_data(self, task_id: str, result: str,
                              result_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """PATCH done with an OPTIONAL structured ``result_data`` payload alongside the plain text.

        Used by the context-request fast path (see ``poller._handle_context_request``) to carry
        card metadata (a list of dicts) back to the backend without overloading the plain-text
        ``result`` field, which every other caller still reads/renders as-is. Omitting
        ``result_data`` (or passing an empty dict/list) is IDENTICAL to plain ``report_done``.
        """
        body: Dict[str, Any] = {"status": "done", "result": result}
        if result_data:
            body["result_data"] = result_data
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_done_with_data failed for task %s: %s", task_id, e)
            return {}

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

    # --- mid-run steering messages (human sends a message while a task runs) --

    def claim_task_messages(self, task_id: str) -> List[Dict[str, Any]]:
        """POST /api/assistant-tasks/{task_id}/messages/claim: ATOMICALLY claim pending messages.

        The backend hands back every message on this task with ``delivered_at == None`` and stamps
        ``delivered_at`` on them in the SAME call, so a message is handed to exactly one caller
        exactly once: a re-poll right after this returns ``[]`` for those same messages (they were
        already delivered). This is what lets a throttled poller call it repeatedly without ever
        folding the same message into two different prompts.

        BEST-EFFORT BY CONTRACT, same shape as ``report_progress`` / ``list_goal_notes``: never
        raises, any API/transport error is logged as a warning and an empty list returned.
        """
        try:
            resp = self._request(
                "POST", f"/api/assistant-tasks/{task_id}/messages/claim") or {}
            return resp.get("messages") or []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("claim_task_messages failed for task %s: %s", task_id, e)
            return []

    # --- live progress into the originating chat -----------------------------

    def post_conversation_message(self, conv_id: str, content: str, *,
                                  kind: str = "progress",
                                  task_id: Optional[str] = None,
                                  card_id: Optional[str] = None) -> Dict[str, Any]:
        """Append a LIVE progress message into the Quest AI conversation a task came from.

        This is how a chat-delegated background task keeps the chat from going silent: the runner
        posts ``started`` when it picks the task up, ``progress`` for real milestones, and ``done``
        with the result. It executes under the quest OWNER's identity (the ``qsk_`` key), and the
        endpoint is owner-scoped, so the runner is simply appending to the owner's own conversation.

        ``task_id``, when given, is stamped on the stored message so the frontend can correlate
        this progress post back to the task's own lifecycle (e.g. to show/hide a "stop" control, or
        group a task's messages together). Omitted from the body when not given, so callers that
        don't have a task in scope behave exactly as before.

        ``card_id`` is a RESERVED, optional key with no behavior yet: when a task carries one,
        it rides on the progress-post body so a future backend can thread a task's posts under a
        per-idea thread in the conversation. Omitted from the body when not given.

        Best-effort by contract at the call site (a dropped progress post must never fail the task),
        but this method itself surfaces API errors so callers can log them.
        """
        try:
            if not self.configured:
                log.warning("post_conversation_message skipped: Quest API not configured")
                return {}
            body: Dict[str, Any] = {"content": content, "kind": kind}
            if task_id is not None:
                body["task_id"] = task_id
            if card_id is not None:
                body["card_id"] = card_id
            return self._request("POST", f"/api/quest-ai/conversations/{conv_id}/progress",
                                 body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("post_conversation_message for conv %s failed: %s", conv_id, e)
            return {}

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
        try:
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to post an environment heartbeat")
            body: Dict[str, Any] = {"capabilities": dict(capabilities)}
            if runner_label:
                body["runner_label"] = runner_label
            if env_id:
                body["env_id"] = env_id
            return self._request("POST", f"/api/teams/{tid}/environment/heartbeat", body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("post_environment_heartbeat failed: %s", e)
            return {}

    # --- escalation (team decision-requests; the confirm-before-act surface) --

    def create_decision(self, summary: str, *, kind: str = "approve",
                        quest_id: Optional[str] = None, assignee_user_id: Optional[str] = None,
                        default_on_silence: str = "hold",
                        team_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to raise a decision-request")
            # Quest stores a decision's summary as a goal CONDITION, capped at 4000 chars server-side.
            # A verbose planner question/clarification (or any caller) can exceed that and the POST is
            # rejected with "Goal condition is limited to 4000 characters". Cap here at the single
            # boundary to Quest so an over-long summary is truncated (never dropped or errored),
            # regardless of which caller built it.
            if isinstance(summary, str) and len(summary) > 4000:
                summary = summary[:3900].rstrip() + "\n\n[...truncated]"
            body: Dict[str, Any] = {"kind": kind, "summary": summary,
                                    "default_on_silence": default_on_silence}
            if quest_id:
                body["quest_id"] = quest_id
            if assignee_user_id:
                body["assigned_to_user_id"] = assignee_user_id
            return self._request("POST", f"/api/teams/{tid}/decisions", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("create_decision failed: %s", e)
            return {}

    def list_open_decisions_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            return self._request("GET", "/api/teams/decisions/for-user",
                                 params={"user_id": user_id}) or []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_open_decisions_for_user failed for user %s: %s", user_id, e)
            return []

    def resolve_decision(self, decision_id: str, resolution: str) -> Dict[str, Any]:
        try:
            return self._request("POST", f"/api/teams/decisions/{decision_id}/resolve",
                                 body={"resolution": resolution})
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("resolve_decision failed for decision %s: %s", decision_id, e)
            return {}

    def list_open_decisions_for_quest(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET the OPEN (unresolved) decisions that reference a specific quest.

        Used by Autopilot's HOLD gate (see ``runner.autopilot``): a quest with an open human
        decision already pending should not have more autonomous work piled onto it.

        The backend route is ``GET /api/teams/decisions/for-quest?quest_id=`` (NOT a team-scoped
        path: it is quest-scoped and derives access from the quest). It returns a BARE LIST of
        ALL the quest's decisions, open AND resolved, with no status filter available server-side
        -- so the open-only narrowing happens HERE, on ``status == "open"``
        (``TeamDecisionStatus.OPEN``; the other values are ``resolved`` / ``expired``).

        Best-effort: returns ``[]`` on any failure (unconfigured client, network, a backend
        without the route) so a gate check can never abort a pass. The CALLER logs the skip
        reason either way, so a quest is never silently passed over.
        """
        try:
            resp = self._request("GET", "/api/teams/decisions/for-quest",
                                 params={"quest_id": quest_id})
            rows = resp if isinstance(resp, list) else list((resp or {}).get("decisions") or [])
            return [d for d in rows
                    if str(d.get("status") or "").strip().lower() == "open"]
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_open_decisions_for_quest failed for quest %s: %s", quest_id, e)
            return []

    # --- quest and goal browsing (for interactive chat context selection) ------

    def list_quests(self, *, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/quests — all quests attached to the team.

        Returns a list of dicts with keys: quest_id, outcome, completed, owner_user_ids.
        Requires team_id either here or on the client instance.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to list quests")
            resp = self._request("GET", f"/api/teams/{tid}/quests") or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quests failed: %s", e)
            return []

    def list_quest_goals(self, quest_id: str, *,
                         team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals — goals grouped by time period.

        Returns {quest_id, outcome, period_groups: [{time_scope, period, period_label, goals: [{id,
        name, time_scope, period, period_label, deadline, completed, parent_goal_id}]}]}.
        Groups are ordered year → quarter → month → week → day → custom, then chronologically.
        Requires team_id either here or on the client instance.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to list quest goals")
            return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}/goals") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_goals failed for quest %s: %s", quest_id, e)
            return {}

    # --- goal creation (a REAL typed Goal, distinct from create_task's AI work item) ----------

    def create_goal(self, title: str, *,
                    period: str,
                    quest_id: Optional[str] = None,
                    description: Optional[str] = None,
                    criteria: Optional[str] = None,
                    goal_type: Optional[str] = None,
                    parent_goal_id: Optional[str] = None,
                    target_value: Optional[float] = None,
                    target_unit: Optional[str] = None,
                    ai_help: Optional[bool] = None,
                    assignee_rep_id: Optional[str] = None) -> Dict[str, Any]:
        """POST a new Goal to /api/planning/goals.

        This is the REAL, typed Goal object shown on a quest's plan (period-scoped, with a
        deadline) -- NOT an assistant task (see ``create_task``, which enqueues AI work and has
        no create_goal equivalent server-side until now). ``quest_id`` is optional: omit it for a
        standalone goal on the caller's own account; a quest-linked goal shows up on that quest's
        plan for every team member regardless of who created it, since the underlying endpoint
        only checks the quest exists, not who owns it.

        ``period`` is REQUIRED and must match one of five formats (validated client-side so a
        typo fails fast with a clear message instead of a 400 from the API):
          day     "YYYY-MM-DD"   e.g. "2026-08-18"
          week    "YYYY_W##"     e.g. "2026_W34"
          month   "YYYY_MM"      e.g. "2026_08"
          quarter "YYYY_Q#"      e.g. "2026_Q3"
          year    "YYYY"         e.g. "2026"
        The deadline is auto-calculated server-side from the period (end of day/week/month/etc).

        Returns the created Goal dict (camelCase fields: id, questId, title, period, deadline,
        ...). Raises ``QuestApiError``/``QuestNotConfigured`` on failure rather than swallowing
        it, same contract as ``create_task``: a caller that tells the user "goal added" must know
        it actually was.
        """
        if not _PERIOD_RE.match(period):
            raise QuestApiError(
                f"Invalid period {period!r}: must be YYYY-MM-DD (day), YYYY_W## (week), "
                f"YYYY_MM (month), YYYY_Q# (quarter), or YYYY (year)."
            )
        body: Dict[str, Any] = {"title": title, "period": period}
        if quest_id is not None:
            body["quest_id"] = quest_id
        if description is not None:
            body["description"] = description
        if criteria is not None:
            body["criteria"] = criteria
        if goal_type is not None:
            body["goal_type"] = goal_type
        if parent_goal_id is not None:
            body["parent_goal_id"] = parent_goal_id
        if target_value is not None:
            body["target_value"] = target_value
        if target_unit is not None:
            body["target_unit"] = target_unit
        if ai_help is not None:
            body["ai_help"] = ai_help
        if assignee_rep_id is not None:
            body["assignee_rep_id"] = assignee_rep_id
        return self._request("POST", "/api/planning/goals", body=body) or {}

    def get_quest(self, quest_id: str, *, team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id} — fetch a single quest by ID.

        Returns quest metadata: quest_id, outcome, completed, owner_user_ids, and other context.
        Requires team_id either here or on the client instance. Returns {} if not found.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to get a quest")
            return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_quest failed for quest %s: %s", quest_id, e)
            return {}

    def get_goal(self, goal_id: str, *, quest_id: Optional[str] = None,
                 team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals/{goal_id} — fetch a single goal by ID.

        Returns goal metadata: id, name, description, deadline, completed, status, and other context.
        Requires team_id and quest_id either as parameters or on the client instance.
        Returns {} if not found.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to get a goal")
            if not quest_id:
                raise QuestNotConfigured("quest_id is required to get a goal")
            return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}/goals/{goal_id}") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_goal failed for goal %s: %s", goal_id, e)
            return {}

    def list_goal_notes(self, goal_id: str, *, quest_id: Optional[str] = None,
                        team_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals/{goal_id}/notes — fetch recent notes.

        Returns a list of note dicts (id, text, author, created_at, etc.). Useful for
        understanding goal progress and context. Returns [] if not found or no notes.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to list goal notes")
            if not quest_id:
                raise QuestNotConfigured("quest_id is required to list goal notes")
            resp = self._request(
                "GET",
                f"/api/teams/{tid}/quests/{quest_id}/goals/{goal_id}/notes",
                params={"limit": limit}
            )
            return list(resp.get("notes") or resp or []) if isinstance(resp, dict) else (list(resp) if isinstance(resp, list) else [])
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_goal_notes failed for goal %s: %s", goal_id, e)
            return []

    # --- account-wide quests (single-user "goal is the hub" lane; NOT team-scoped) --
    # A person's own quests (dissertation, career, family, ...) live on their account, not
    # attached to any team — distinct from list_quests()/get_quest() above, which browse a
    # TEAM's initiative quests. Any owner or active-share holder can read/append with their own
    # key; no team_id is needed or accepted by these endpoints.

    def list_my_quests(self) -> List[Dict[str, Any]]:
        """GET /api/quests/me — every quest the authenticated user owns, account-wide.

        Each item is ``{quest_id, state: {...}}`` with the full quest state embedded (outcome,
        current_state, strategies, notes, ...). This is where a person's real goals live —
        distinct from a team's initiative quests (``list_quests``). Returns [] on any failure.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/quests/me") or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_my_quests failed: %s", e)
            return []

    def get_my_quest(self, quest_id: str) -> Dict[str, Any]:
        """GET /api/quests/{quest_id}/state — fetch one account-wide quest's full state.

        Unlike ``get_quest`` (team-scoped), this needs no team_id. Returns {} if not found or the
        caller lacks access (owner or active share).
        """
        try:
            self._require()
            resp = self._request("GET", f"/api/quests/{quest_id}/state") or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_my_quest failed for quest %s: %s", quest_id, e)
            return {}

    def list_quest_notes(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET /api/quests/{quest_id}/notes — the goal's freeform notes, oldest -> newest.

        The "goal is the hub" surface: any owner or active-share holder can read them. Returns []
        if not found or inaccessible.
        """
        try:
            self._require()
            resp = self._request("GET", f"/api/quests/{quest_id}/notes") or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_notes failed for quest %s: %s", quest_id, e)
            return []

    def add_quest_note(self, quest_id: str, text: str) -> List[Dict[str, Any]]:
        """POST /api/quests/{quest_id}/notes — append a note; returns the updated notes list.

        Attribution is derived server-side from the caller: an API-key caller (this client) is
        recorded ``author_kind: "ai"``. Returns [] on failure.
        """
        try:
            self._require()
            resp = self._request(
                "POST", f"/api/quests/{quest_id}/notes", body={"text": text}) or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_quest_note failed for quest %s: %s", quest_id, e)
            return []

    # --- task creation (enqueue a new AI task) --------------------------------

    def create_task(self, text: str, *,
                    team_id: Optional[str] = None,
                    goal_id: Optional[str] = None,
                    scheduled_at: Optional[str] = None,
                    source: str = "chat",
                    env_id: Optional[str] = None,
                    task_kind: Optional[str] = None,
                    card_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """POST a new queued AI task to /api/assistant-tasks.

        ``team_id`` routes the task to a specific team's runner (defaults to the client's
        configured team). ``goal_id`` attaches the task to a goal so results appear on it -- note
        the Quest API resolves ``goal_id`` as a QUEST id (its handler loads the quest by this id),
        so this single field is BOTH the quest link and the goal link; there is no separate
        ``quest_id`` field on the create route. ``scheduled_at`` is an ISO-8601 UTC datetime
        string; omit it to run as soon as the runner's next poll picks it up. ``source`` must be a
        value the Quest API accepts (chat / reflection / review; "chat" fits an interactive send -
        the old default "cli" was rejected with a 400 by the API, so every CLI enqueue silently
        failed).

        ``env_id`` pins the task to one of the team's connected runner environments (e.g. a
        quest's configured ``autopilot.env_id``); omit to let the backend's normal env routing
        apply.

        ``task_kind`` is the PERSISTENT routing classification (e.g. ``"autopilot"`` for the
        recurring autopilot pass task). Unlike ``handler`` -- which the claim path OVERWRITES on
        every claim with the claiming worker's own label -- ``task_kind`` is written once at create
        and never touched by the claim/status/progress paths, so a poller can route on it reliably
        even across a recurring series' spawned occurrences. Route autopilot on THIS, not on
        ``handler``.

        ``card_ids`` attaches existing context cards (from the same card store the retrieval
        adapters manage, e.g. ``FileContextStore``/``QuestApiCardRepository``) to the task as
        relevant context, either explicitly chosen by the caller or resolved from the task text
        via a card search. Omit or pass an empty list to send no cards, matching prior behavior.

        NOTE: the create route accepts NO ``status`` field (the backend always creates the task
        queued and fills status server-side) and no persona/rep field. A caller that needs a
        different initial status (e.g. ``"suggested"``) must create the task and then PATCH it
        via ``update_task``; see ``runner.autopilot``.

        Returns the created task dict (includes its ``id``).

        Raises ``QuestApiError`` / ``QuestNotConfigured`` on failure instead of swallowing it:
        a caller that acknowledges the user ("I'm looking into it") after calling this MUST
        know whether the task actually exists, otherwise the ack is a promise nothing will
        ever fulfill (the exact silent-failure mode the reliability work bans).
        """
        body: Dict[str, Any] = {"text": text, "source": source}
        tid = team_id if team_id is not None else self.team_id
        if tid:
            body["team_id"] = tid
        if goal_id is not None:
            body["goal_id"] = goal_id
        if scheduled_at is not None:
            body["scheduled_at"] = scheduled_at
        if env_id is not None:
            body["env_id"] = env_id
        if task_kind is not None:
            body["task_kind"] = task_kind
        if card_ids:
            body["card_ids"] = card_ids
        return self._request("POST", "/api/assistant-tasks", body=body) or {}

    def update_task(self, task_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH arbitrary updatable fields on a task (the generic write the report_* helpers wrap).

        The Quest API's task PATCH accepts ``status`` (queued|in_progress|done|needs_you|failed|
        cancelled|suggested), ``result``, ``decision_id``, ``scheduled_date``/``scheduled_time``,
        ``text``, ``recurrence``, ``team_id``, ``goal_id``, ``handler``, ``authored_request``, and
        ``task_kind``. Used by Autopilot to flip a freshly created task to ``"suggested"`` (the
        create route has no ``status`` field, so create-then-PATCH is the only way to land a task
        in the suggested state).

        RAISES on failure rather than swallowing: the caller that just created a task and is
        demoting it to ``suggested`` must know if that demotion failed, otherwise a proposal the
        user never approved would sit in the queue and RUN. Silent failure here is not acceptable.
        """
        return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=dict(fields)) or {}

    def list_tasks(self, *, team_id: Optional[str] = None,
                  status: Optional[str] = None,
                  goal_id: Optional[str] = None,
                  source: Optional[str] = None,
                  task_kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/assistant-tasks, for Autopilot's budget/backpressure math.

        SERVER-SIDE filters (the only ones the Quest API's list route actually implements):
        ``status``, ``goal_id``, ``team_id``. Everything else it ignores -- there is no
        ``source``, ``task_kind``, or ``quest_id`` query parameter -- and an unknown query param
        is silently dropped by FastAPI rather than erroring, which would hand a caller a
        SUPERSET it mistook for a filtered set.

        So ``source`` and ``task_kind`` are applied CLIENT-SIDE here, on the returned rows. This
        is the honest split: the method's contract is "these filters hold on the result", and it
        does whatever the backend won't. (``goal_id`` on a task IS the quest id -- see
        ``create_task`` -- so filtering by a quest means passing that quest id as ``goal_id``.)

        Returns ``[]`` on any failure. Never raises.
        """
        tid = team_id if team_id is not None else self.team_id
        params: Dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if goal_id is not None:
            params["goal_id"] = goal_id
        if tid:
            params["team_id"] = tid
        try:
            resp = self._request("GET", "/api/assistant-tasks", params=params)
            tasks = _as_task_list(resp)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_tasks failed: %s", e)
            return []
        if source is not None:
            tasks = [t for t in tasks if t.get("source") == source]
        if task_kind is not None:
            tasks = [t for t in tasks if t.get("task_kind") == task_kind]
        return tasks

    # --- quest autopilot config -----------------------------------------------

    def get_quest_autopilot(self, quest_id: str) -> Dict[str, Any]:
        """Read ONE quest's full state, for its ``autopilot`` settings + ``outcome``.

        WHY THIS EXISTS (and why the team quest LIST is not enough): the team's quest listing
        (``list_quests`` -> ``GET /api/teams/{team_id}/quests``) returns only
        ``{quest_id, outcome, completed, owner_user_ids}``. It does NOT carry ``autopilot``. A
        scanner that read the opt-in mode off those rows would see every quest as mode "off" and
        do nothing, forever, without a single error -- precisely the silent-failure class this
        codebase bans. The ``autopilot`` block is serialized on the full QuestState, which is what
        ``GET /api/quests/{quest_id}/state`` returns.

        Returns the quest STATE dict (``{outcome, completed, autopilot: {...}, ...}``), or ``{}``
        when the quest is missing/inaccessible or the read fails. Never raises.
        """
        try:
            resp = self.get_my_quest(quest_id) or {}
            state = resp.get("state") if isinstance(resp, dict) else None
            return state if isinstance(state, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_quest_autopilot failed for quest %s: %s", quest_id, e)
            return {}

    def update_quest_autopilot(self, quest_id: str,
                              fields: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH a quest's autopilot settings -- ``PATCH /api/quests/{quest_id}/autopilot``.

        The route is QUEST-scoped (no team id) and its body is FLAT (``{"mode": ..., "cadence":
        ...}``), NOT nested under an ``autopilot`` key. It MERGES server-side: the handler reads
        the quest's current AutopilotSettings and overwrites only the fields present in the
        request, so a partial update cannot wipe sibling fields.

        IMPORTANT -- the write path is currently NARROWER than the model. The endpoint's request
        schema accepts only ``mode``, ``planning``, ``cadence``, ``personas``, and ``env_id``. The
        scanner's own bookkeeping fields (``last_pass_at``, ``miss_streak``, ``helpful``,
        ``unhelpful``) EXIST on the stored model but are not accepted by this endpoint, and an
        unknown key is silently ignored by its Pydantic model -- so PATCHing them returns 200 and
        persists NOTHING. The caller must therefore VERIFY, not assume (see
        ``runner.autopilot._update_pass_bookkeeping``, which reads the echoed settings back and
        reports loudly when a field it wrote did not stick, rather than letting the cadence gate
        silently never advance).

        Returns the endpoint's response (``{"quest_id": ..., "autopilot": {...}}``), or ``{}`` on
        failure. Never raises: a bookkeeping write failing must not break the pass that produced
        the work.
        """
        try:
            return self._request("PATCH", f"/api/quests/{quest_id}/autopilot",
                                 body=dict(fields)) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("update_quest_autopilot failed for quest %s: %s", quest_id, e)
            return {}

    # --- AI-rep profile (the rep <-> skill-file sync surface) -----------------

    def get_ai_profile(self, user_id: str, *, team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET ALL data for a team member's AI rep.

        Returns ``{user_id, display_name, persona, learned_notes: [{id, text, created_at,
        source, message_id?}], updated_at}``. This is the single source of truth the runner
        renders into the rep's local Claude skill file(s). ``team_id`` defaults to the client's
        configured team.
        """
        try:
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to read an AI-rep profile")
            return self._request("GET", f"/api/teams/{tid}/members/{user_id}/ai-profile") or {}
        except QuestNotConfigured:
            raise
        except QuestApiError as e:
            # 404 = this member simply has no per-team AI rep profile (e.g. a registry-rep-only
            # executor). An expected state on every poll, not a warning.
            if getattr(e, "status", None) == 404:
                log.debug("no ai-profile for user %s on team: %s", user_id, e)
            else:
                log.warning("get_ai_profile failed for user %s: %s", user_id, e)
            return {}

    def update_ai_profile(self, user_id: str, *, display_name: Optional[str] = None,
                          persona: Optional[str] = None,
                          learned_notes: Optional[list] = None,
                          team_id: Optional[str] = None) -> Dict[str, Any]:
        """PUT edits to a rep's profile (display_name and/or persona).

        NOTE: learned_notes are no longer stored on the rep profile. Feedback/corrections
        are stored as guidance cards (via add_rep_correction) and loaded via list_guidance_cards.

        Only the fields you pass are sent, so a local edit to just the persona pushes up only the
        persona. Returns the updated profile. ``team_id`` defaults to the client's configured team.
        """
        try:
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
        except (QuestApiError, QuestNotConfigured) as e:
            if getattr(e, "status", None) == 404:
                log.debug("no ai-profile to edit for user %s on team: %s", user_id, e)
            else:
                log.warning("update_ai_profile failed for user %s: %s", user_id, e)
            return {}

    def add_rep_correction(self, user_id: str, correction: str, *,
                           message_id: Optional[str] = None,
                           task_type: Optional[str] = None,
                           team_id: Optional[str] = None) -> Dict[str, Any]:
        """POST a rep correction as a guidance card (not as rep.learned_notes).

        Feedback is stored as guidance cards in the Quest guidance collection,
        not as rep-specific notes. This makes feedback available to all reps
        while allowing rep-specific filtering via tags.

        Args:
            user_id: The rep being corrected.
            correction: The feedback/correction text.
            message_id: Optional chat message reference.
            task_type: Optional task type (plan, answer, deep, etc.).
            team_id: Team ID (defaults to client's configured team).

        Returns:
            The created guidance card dict {id, title, body, tags, ...}.
        """
        try:
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to add a rep correction")

            body: Dict[str, Any] = {"correction": correction}
            if message_id is not None:
                body["message_id"] = message_id
            if task_type is not None:
                body["task_type"] = task_type

            return self._request(
                "POST",
                f"/api/teams/{tid}/members/{user_id}/corrections",
                body=body,
            ) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_rep_correction failed for user %s: %s", user_id, e)
            return {}

    def list_guidance_cards(self, *, rep_id: Optional[str] = None,
                            source: Optional[str] = None,
                            task_type: Optional[str] = None,
                            team_id: Optional[str] = None,
                            limit: int = 100) -> List[Dict[str, Any]]:
        """GET guidance cards for this team, optionally filtered by rep/source/task.

        Returns guidance cards that can be loaded by the dynamic loader for
        UniversalGuidanceProvider. Cards can be filtered by:
        - rep_id: guidance specific to this rep (rep:rep_id tag)
        - source: guidance from this source (source:feedback, source:correction, etc.)
        - task_type: guidance for this task type (task:plan, task:answer, etc.)

        Returns: List of guidance card dicts {id, title, body, tags, description, ...}.
        """
        try:
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to list guidance cards")

            params: Dict[str, Any] = {"limit": limit}
            if rep_id:
                params["rep_id"] = rep_id
            if source:
                params["source"] = source
            if task_type:
                params["task_type"] = task_type

            resp = self._request(
                "GET",
                f"/api/teams/{tid}/guidance-cards",
                params=params,
            ) or {}
            return list(resp.get("cards") or resp or []) if isinstance(resp, dict) else (
                list(resp) if isinstance(resp, list) else []
            )
        except QuestApiError as e:
            if e.status == 404:
                log.info("list_guidance_cards: endpoint not available on this backend (404) — skipping")
            else:
                log.warning("list_guidance_cards failed: %s", e)
            return []
        except QuestNotConfigured as e:
            log.warning("list_guidance_cards failed: %s", e)
            return []


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
        try:
            res = self._client.create_decision(
                escalation.summary,
                kind=escalation.kind,
                quest_id=escalation.quest_id,
                assignee_user_id=escalation.assignee or self._default_assignee,
                default_on_silence=escalation.default_on_silence,
            )
            # The API returns the created decision; surface its id (best-effort across field names).
            return str((res or {}).get("decision_id") or (res or {}).get("id") or "")
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("escalate failed: %s", e)
            return ""
