"""Assistant tasks: discovery, claim, reporting, requeue, messages, heartbeat and task CRUD.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import urllib.request
from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime, timezone

from ..core.work_model import RUNNABLE_MODELS
from .quest_client_support import QuestApiError, QuestNotConfigured, _as_task_list, log


class TasksMixin:
    """Assistant tasks: discovery, claim, reporting, requeue, messages, heartbeat and task CRUD."""

    def discover_due(self, *, now: Optional[datetime] = None,
                     status: str = "queued",
                     team_id: Optional[str] = None,
                     team_ids: Optional[Sequence[str]] = None,
                     env_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET queued tasks due at/before now (the poll-mode discovery floor).

        Unscheduled tasks count as 'due now'; scheduled ones surface within their window.

        ``team_id`` scopes discovery to ONE team's tasks. ``team_ids`` scopes it to a SET of teams
        in one request (see ``team_param``), which is how a single lane serves several teams.

        Why scope at all: several teams under the SAME owner share one owner-scoped queue, so an
        unfiltered lane sees every one of them. The hazard that makes that dangerous is two
        separate PROCESSES racing one shared queue -- the claim-before-run PATCH to
        ``in_progress`` is an unconditional set, not a status-guarded find-and-update, so two
        processes really can both claim the same task and run it twice. It is NOT a single
        process legitimately serving several teams: each task is claimed once, by one process, and
        carries its own ``team_id`` that the per-task path resolves persona, corpus and escalation
        from. So run one process per QUEUE, not one per team, and give that process the set of
        teams it should serve.

        Defaults to the client's configured ``team_id`` so a team-bound lane is isolated by
        default; pass ``team_id=""`` (or configure none) for an owner-scoped, teamless discovery.
        The backend treats a task's null ``team_id`` as owner-scoped, so a team filter only
        narrows, never breaks the personal lane.

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
        tid = self.team_param(team_id, team_ids)
        params: Dict[str, Any] = {"status": status, "due_before": iso}
        if status == "queued":
            # Executable work only: the server returns queued tasks whose start time has come.
            params["for_execution"] = "true"
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
                             team_ids: Optional[Sequence[str]] = None,
                             env_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET queued REAL-TIME tasks only (the fast-lane FALLBACK poll).

        Real-time tasks are ones a task-creation path flagged as low-latency-eligible (a live chat
        context-request and similar) because a caller is waiting on a reply right now -- see
        ``wait_for_interactive`` for the preferred, lower-latency long-poll channel. This method is
        the fallback used when the wait channel is disabled (``QAR_WAIT_CHANNEL=0``): the fast lane
        calls it on a short interval (``QAR_CONTEXT_POLL_SECONDS``) instead of waiting out the full
        background ``poll_interval_seconds``. Same team/env scoping as ``discover_due``, including
        ``team_ids`` for a lane serving several teams. Never raises.
        """
        tid = self.team_param(team_id, team_ids)
        params: Dict[str, Any] = {"status": "queued", "real_time": "true", "for_execution": "true"}
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
                             team_ids: Optional[Sequence[str]] = None,
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

        ``team_ids`` (a lane serving several teams) MUST go into this ONE call rather than a call
        per team. This endpoint returns exactly one FIFO-oldest task for the scope it was given,
        so a lane that asked for one team and filtered the rest client-side would, on every
        reconnect, be handed a task it then discards -- a silent drop plus a hot reconnect loop
        for as long as that task sits at the head of the queue.
        """
        tid = self.team_param(team_id, team_ids)
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

    @staticmethod
    def _with_session(body: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        """Add the run's ``session_id`` to a terminal report body, when there is one.

        THE THREAD'S IDENTITY OUTSIDE ONE PROCESS. A deep run's Claude session used to live and die
        inside one call: the id existed on the ``DeepResult`` and was never told to anyone, so a run
        tomorrow could not pick up the session a run today left behind. Reporting it here is what
        lets a backend store it on the thread and hand it back as ``resume_session_id`` on the next
        run (see ``TaskExecutor.execute``).

        Additive and optional in both directions: an empty/missing id sends NOTHING (never an empty
        string), so a backend that knows nothing about the field receives byte-for-byte the body it
        received before, and a backend that does know about it simply learns nothing new from a run
        that had no session (a crash, a non-subprocess deep runner, a plain answer turn).
        """
        sid = (session_id or "").strip()
        if sid:
            body["session_id"] = sid
        return body

    def report_done(self, task_id: str, result: str,
                    session_id: Optional[str] = None,
                    result_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """PATCH done. ``result_data`` carries ``{"outcome": "done" | "nothing_done"}``: whether the
        run made at least one successful mutating call ("done") or only talked ("nothing_done"), so
        a consumer that needs the work done (a workflow step) can tell the two apart."""
        body: Dict[str, Any] = {"status": "done", "result": result}
        if result_data:
            body["result_data"] = result_data
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body=self._with_session(body, session_id))
        except QuestApiError as e:
            # A backend that predates ``result_data`` refuses the whole PATCH (422, extra field),
            # which left a finished task "in progress" forever. The outcome stamp is an extra: report
            # the task done without it rather than not at all.
            if getattr(e, "status", None) == 422 and "result_data" in body and "result_data" in str(e):
                log.warning("backend does not accept result_data on a task report; reporting task "
                            "%s done without it", task_id)
                body.pop("result_data", None)
                try:
                    return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                         body=self._with_session(body, session_id))
                except (QuestApiError, QuestNotConfigured) as e2:
                    log.warning("report_done failed for task %s: %s", task_id, e2)
                    return {}
            log.warning("report_done failed for task %s: %s", task_id, e)
            return {}
        except QuestNotConfigured as e:
            log.warning("report_done failed for task %s: %s", task_id, e)
            return {}

    def report_needs_you(self, task_id: str, result: str, decision_id: str,
                         session_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body=self._with_session(
                                     {"status": "needs_you", "result": result,
                                      "decision_id": decision_id}, session_id))
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_needs_you failed for task %s: %s", task_id, e)
            return {}

    def report_incomplete(self, task_id: str, result: str,
                          session_id: Optional[str] = None) -> Dict[str, Any]:
        """PATCH ``incomplete``: the run WORKED but did not fully reach its goal (or could not be
        verified as having reached it). Distinct from ``failed``, which means a technical error.
        """
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body=self._with_session(
                                     {"status": "incomplete", "result": result}, session_id))
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_incomplete for task %s: %s", task_id, e)
            return {}

    def report_failed(self, task_id: str, result: str,
                      session_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}",
                                 body=self._with_session(
                                     {"status": "failed", "result": result}, session_id))
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_failed for task %s: %s", task_id, e)
            return {}

    def report_done_with_data(self, task_id: str, result: str,
                              result_data: Optional[Dict[str, Any]] = None,
                              session_id: Optional[str] = None) -> Dict[str, Any]:
        """PATCH done with an OPTIONAL structured ``result_data`` payload alongside the plain text.

        Used by the context-request fast path (see ``poller._handle_context_request``) to carry
        card metadata (a list of dicts) back to the backend without overloading the plain-text
        ``result`` field, which every other caller still reads/renders as-is. Omitting
        ``result_data`` (or passing an empty dict/list) is IDENTICAL to plain ``report_done``.
        """
        body: Dict[str, Any] = self._with_session({"status": "done", "result": result}, session_id)
        if result_data:
            body["result_data"] = result_data
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("report_done_with_data failed for task %s: %s", task_id, e)
            return {}

    def requeue_for_usage_limit(self, task_id: str, *, start_at: datetime, detail: str,
                                resume_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Put a CLAIMED task back in the queue to wait out the Claude usage limit.

        PATCHes ``status=queued`` with ``start_at`` (when to resume, the parsed reset time) and
        ``hold_reason=claude_limit``; the backend then keeps every lane off the task until then
        (its claim guard and discovery filter), counts the waiting episode, and notifies the owner
        once. ``resume_session_id`` hands over the Claude session the stopped run had open, so the
        resumed run continues that work rather than starting over. Returns the task, or None when
        the write failed (the caller must then report the task some other way, since it is still
        marked in_progress and nothing else will pick it up).
        """
        return self.requeue_with_hold("claude_limit", task_id, start_at=start_at, detail=detail,
                                      resume_session_id=resume_session_id)

    def requeue_for_provider_outage(self, task_id: str, *, start_at: datetime, detail: str,
                                    resume_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Put a CLAIMED task back in the queue to wait out a model-provider outage.

        The same put-back as ``requeue_for_usage_limit`` with ``hold_reason=provider_unavailable``:
        the backend keeps every lane off the task until ``start_at`` and shows the wait on it. A
        backend that refuses the reason returns None, and the caller reports the task failed rather
        than letting it go straight back to a claim.
        """
        return self.requeue_with_hold("provider_unavailable", task_id, start_at=start_at,
                                      detail=detail, resume_session_id=resume_session_id)

    def requeue_with_hold(self, hold_reason: str, task_id: str, *, start_at: datetime, detail: str,
                          resume_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """PATCH a claimed task back to queued, held until ``start_at`` for ``hold_reason``.

        Returns the task, or None when the write failed (see ``requeue_for_usage_limit``)."""
        body: Dict[str, Any] = {"status": "queued", "start_at": self.utc_stamp(start_at),
                                "hold_reason": hold_reason, "hold_detail": (detail or "")[:500]}
        sid = (resume_session_id or "").strip()
        if sid:
            body["resume_session_id"] = sid
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("requeue (%s) failed for task %s: %s", hold_reason, task_id, e)
            return None

    def hold_for_usage_limit(self, task_id: str, *, start_at: datetime,
                             detail: str) -> Optional[Dict[str, Any]]:
        """Hold a still-QUEUED task until the lane's usage-limit pause ends, without claiming it.

        Used by the lane pause: work that arrives while Claude Code is limited is not run (it would
        only be refused), and holding it makes the wait visible on the task ("Waiting for Claude
        Code, resumes ...") and enforced server-side. Returns the task, or None on failure.
        """
        body = {"start_at": self.utc_stamp(start_at), "hold_reason": "claude_limit",
                "hold_detail": (detail or "")[:500]}
        try:
            return self._request("PATCH", f"/api/assistant-tasks/{task_id}", body=body)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("hold_for_usage_limit failed for task %s: %s", task_id, e)
            return None

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

    def open_run_on_task(self, task_id: str, text: str) -> Dict[str, Any]:
        """POST /api/assistant-tasks/{task_id}/iterate: open a NEW run on this task's thread.

        Used for messages the running run never folded in: once the task is terminal, the thread
        takes a fresh run carrying ``text`` and the runner picks it up, so the person gets a second
        output rather than a message that was received and then ignored. Unlike the best-effort
        reads above this RAISES on failure, because the caller must tell the person the message did
        not start anything; a silent no-op here would be the same ignored-message bug again.
        """
        return self._request("POST", f"/api/assistant-tasks/{task_id}/iterate",
                             body={"text": text})

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
                                   team_id: Optional[str] = None,
                                   org_id: Optional[str] = None) -> Dict[str, Any]:
        """Tell the backend this runner is ALIVE and what it can do (the env heartbeat).

        By default POSTs to ``/api/teams/{team_id}/environment/heartbeat`` with the runner's
        declared ``capabilities`` ({web, corpus, code}). The backend stamps last_heartbeat_at,
        stores the reported capabilities, and auto-registers the team's env config on first
        contact — making the env queryable by the routing classifier. Authed by THIS runner's
        qsk_ key (its user must be a member of the team). Returns the stored env state.

        Pass ``org_id`` to register at ORG scope instead: POSTs to
        ``/api/orgs/{org_id}/environment/heartbeat`` (same body shape) so this runner becomes
        available to EVERY team in that org, not just one team. When ``org_id`` is truthy,
        ``team_id`` is NOT required (org-scope heartbeats don't need one).

        ``env_id`` identifies WHICH of the team's (or org's) environments this runner is — a
        team/org can attach several runners, each its own environment. Omit it and this runner is
        the DEFAULT environment (so a single-runner deployment needs no extra config). ``team_id``
        defaults to the client's configured team. The CALLER (poller) keeps this best-effort: a
        failed heartbeat must never break task execution.
        """
        try:
            body: Dict[str, Any] = {"capabilities": dict(capabilities)}
            # The work models this runner can run (Claude Code deep worker). The backend narrows the
            # environment's configured options to these, so an org never offers a model the runner lacks.
            body["available_models"] = [{"provider": "anthropic", "model": m} for m in RUNNABLE_MODELS]
            # The direct tools this runner has (read-only listing set by the poller), so the backend can
            # offer each as an action. Omitted when none are known, which leaves the last report alone.
            tools = getattr(self, "available_tools", None)
            if tools:
                body["available_tools"] = list(tools)
            if runner_label:
                body["runner_label"] = runner_label
            if env_id:
                body["env_id"] = env_id
            if org_id:
                path = f"/api/orgs/{org_id}/environment/heartbeat"
            else:
                tid = team_id or self.team_id
                if not tid:
                    raise QuestNotConfigured("team_id is required to post an environment heartbeat")
                path = f"/api/teams/{tid}/environment/heartbeat"
            return self._request("POST", path, body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("post_environment_heartbeat failed: %s", e)
            return {}

    def publish_action_catalog(self, owner_type: str, owner_id: str, publisher_id: str,
                               actions: List[Dict[str, Any]], *, env_id: Optional[str] = None,
                               publisher_label: Optional[str] = None) -> Dict[str, Any]:
        """Publish the actions this runner offers to Quest Workflows for a team or org.

        PUTs ``/api/{teams|orgs}/{owner_id}/action-catalogs/{publisher_id}``, replacing what this
        publisher published there before. Authed by this runner's qsk_ key (its user must be a member).
        Returns Quest's summary (``action_count``, ``unchanged``, ...), or ``{}`` on failure: a failed
        publish never breaks the poll, the next heartbeat tries again."""
        plural = "orgs" if owner_type == "org" else "teams"
        body: Dict[str, Any] = {"actions": actions}
        if env_id:
            body["env_id"] = env_id
        if publisher_label:
            body["publisher_label"] = publisher_label
        try:
            return self._request("PUT", f"/api/{plural}/{owner_id}/action-catalogs/{publisher_id}", body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("publish_action_catalog failed for %s %s: %s", owner_type, owner_id, e)
            return {}

    def get_work_model_config(self, *, team_id: Optional[str] = None,
                              env_id: Optional[str] = None) -> Dict[str, Any]:
        """The effective work-model view for one of the team's environments.

        GETs ``/api/teams/{team_id}/environments/{env_id}/work-model``. The backend resolves the
        team override, else the org's config for that env, else the Claude default, and returns it
        under ``effective`` (see ``quest_ai_runner.core.work_model.apply_work_model``). ``env_id``
        omitted means the default environment. Raises on failure: the caller decides the fallback.
        """
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to read the work-model configuration")
        eid = urllib.parse.quote(env_id or self.env_id or "default", safe="")
        return self._request("GET", f"/api/teams/{tid}/environments/{eid}/work-model") or {}

    # --- task creation (enqueue a new AI task) --------------------------------

    def create_task(self, text: str, *,
                    team_id: Optional[str] = None,
                    goal_id: Optional[str] = None,
                    scheduled_at: Optional[str] = None,
                    scheduled_date: Optional[str] = None,
                    scheduled_time: Optional[str] = None,
                    source: str = "chat",
                    title: Optional[str] = None,
                    env_id: Optional[str] = None,
                    task_kind: Optional[str] = None,
                    status: Optional[str] = None,
                    recurrence: Optional[Any] = None,
                    assignee_rep_id: Optional[str] = None,
                    assignee_user_id: Optional[str] = None,
                    parent_task_id: Optional[str] = None,
                    card_ids: Optional[List[str]] = None,
                    model: Optional[str] = None,
                    deep_run_model: Optional[str] = None) -> Dict[str, Any]:
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

        ``model`` is the per-task QAR-call TIER override (e.g. "fast"/"balanced"/"quality"/"best").
        ``deep_run_model`` is a separate, literal pin for the expensive DEEP RUN execution (e.g. a
        bare Claude family alias like "opus"/"sonnet"/"haiku"/"fable", or a pinned id) -- the two
        are independent knobs on the consumer backend's task schema and must not be conflated: a
        caller holding a single configured value (e.g. a quest's ``autopilot.model``) must classify
        it itself (tier name vs. literal model name) and pass it via the matching parameter, since
        the backend validates each field against its own distinct allowed set and 400s a tier name
        sent as ``deep_run_model`` or a literal model sent as ``model``. Omit either to let the
        executor's own default apply. The accepted vocabulary for both is the consumer backend's,
        not this library's -- pass a value that backend's task-creation endpoint accepts.

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

        ``status`` lands the task in its initial state ATOMICALLY. Only ``"queued"`` (the default)
        and ``"suggested"`` may be asserted at creation. Use ``"suggested"`` for anything a human
        must approve before it runs: creating it queued and PATCHing it down afterwards leaves a
        window in which the runner's poll can claim and EXECUTE the task before the demotion
        lands, which is precisely the approval that suggest mode exists to require.

        ``recurrence`` makes this the first occurrence of a repeating series (the backend stamps a
        ``series_id`` and re-queues the next occurrence whenever one reaches a terminal status).
        Either free text (``"daily"``) or a structured object
        (``{"frequency": "daily", "time": "07:00"}``). Pair it with ``scheduled_date`` /
        ``scheduled_time`` ('YYYY-MM-DD' / 'HH:MM') to fix when occurrences fire.

        ``parent_task_id`` links this task to the task that CREATED it (e.g. the autopilot pass
        that scheduled a piece of work). It is what lets a caller answer "what came of that run"
        with the child's own output instead of a list of ids, and it is how the Quest API threads a
        follow-up onto its parent. Omit it for a task nothing else authored.

        ``assignee_rep_id`` carries the PERSONA structurally (e.g. the rep id a quest's
        ``autopilot.personas`` roster assigns to today). It does not change which lane executes the
        task -- that is the quest owner / ``assignee_user_id`` -- it states which character voice
        should carry it, so a consumer's resolver reads a field instead of parsing prose.

        ``assignee_user_id`` is the other half of that pair and the one with teeth: it sets the
        task's EXECUTOR, which the backend stores as the task's own ``user_id`` (its owner), in
        place of the default executor (the linked quest's OWNER, or the caller for a personal
        task). That matters because task discovery is owner-scoped: a lane only ever sees tasks its
        own account owns, so work an app account creates on a quest a HUMAN owns is invisible to
        that lane and never runs unless the account assigns the task to itself. The backend
        requires a team (the quest's team, or ``team_id`` on a personal task) and that the assignee
        is a live member of it: a non-member is a 403, no team at all is a 400. Omit it to keep the
        default executor.

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
        if title is not None:
            # Without this the server derives a title from the first line of ``text``, which for an
            # autopilot batch is its "Act as ..." persona line, so every task in the list ends up
            # named after its persona rather than its work.
            body["title"] = title
        if scheduled_date is not None:
            body["scheduled_date"] = scheduled_date
        if scheduled_time is not None:
            body["scheduled_time"] = scheduled_time
        if env_id is not None:
            body["env_id"] = env_id
        if task_kind is not None:
            body["task_kind"] = task_kind
        if status is not None:
            body["status"] = status
        if recurrence is not None:
            body["recurrence"] = recurrence
        if assignee_rep_id is not None:
            body["assignee_rep_id"] = assignee_rep_id
        if assignee_user_id is not None:
            body["assignee_user_id"] = assignee_user_id
        if parent_task_id is not None:
            body["parent_task_id"] = parent_task_id
        if card_ids:
            body["card_ids"] = card_ids
        if model is not None:
            body["model"] = model
        if deep_run_model is not None:
            body["deep_run_model"] = deep_run_model
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

        OWNER SCOPING, and why ``goal_id`` is not just a filter (2026-09-17). Without
        ``goal_id`` the route answers from the caller's OWN task list: the backend's
        ``list_assistant_tasks`` scopes on the authenticated ``user_id``, and ``team_id``
        NARROWS that set rather than widening it. With ``goal_id`` it answers from
        ``list_assistant_tasks_for_goal``, which is not scoped to any user at all (access is
        checked once, as owner-or-live-team-member). That matters because a task created against
        a quest is OWNED by the quest's owner while ``created_by`` records the submitter: an app
        account that creates work on a human's quest cannot see that work again in the team-wide
        list, only in the per-quest one. So a "does this quest already have X" question MUST pass
        ``goal_id``; the team-wide list can only answer it for quests the calling account owns.

        Returns ``[]`` on any failure. Never raises. When the caller has to tell a real empty
        list from a failed read (anything that CREATES on an empty answer), call
        ``list_tasks_or_none`` instead.
        """
        return self.list_tasks_or_none(team_id=team_id, status=status, goal_id=goal_id,
                                       source=source, task_kind=task_kind) or []

    def list_tasks_or_none(self, *, team_id: Optional[str] = None,
                           status: Optional[str] = None,
                           goal_id: Optional[str] = None,
                           source: Optional[str] = None,
                           task_kind: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """``list_tasks``, but a FAILED read is reported instead of swallowed.

        Same filters and the same owner-scoping contract as ``list_tasks`` (read its docstring
        first). The only difference is the failure value: ``None`` on
        ``QuestApiError``/``QuestNotConfigured``, ``[]`` when the read genuinely succeeded and
        matched nothing. Never raises, matching ``list_tasks``/``list_asks``.

        WHY (incident, 2026-09-17): the autopilot pass liveness check read an empty list as "no
        pass exists" and created one. Under a rate-limit burst (429s, hundreds in half an hour)
        every scan read an empty list, so every scan created another recurring pass series. Any
        caller whose next move on an empty answer is a WRITE has to use this method: "nothing is
        there" and "I could not look" must never be the same value in front of a create.
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
            return None
        if source is not None:
            tasks = [t for t in tasks if t.get("source") == source]
        if task_kind is not None:
            tasks = [t for t in tasks if t.get("task_kind") == task_kind]
        return tasks
