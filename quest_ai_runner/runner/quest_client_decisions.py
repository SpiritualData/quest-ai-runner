"""Decisions and asks: propose, create, resolve and list.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from datetime import datetime

from .quest_experiments import EXPERIMENT_PROPOSAL_KIND
from .quest_client_support import DECISION_KIND_RE, QuestApiError, QuestNotConfigured, log


class DecisionsMixin:
    """Decisions and asks: propose, create, resolve and list."""

    # --- escalation (team decision-requests; the confirm-before-act surface) --

    def propose_field_change(self, target_type: str, target_id: str,
                             changes: List[Dict[str, Any]], *,
                             why: str = "",
                             quest_id: Optional[str] = None,
                             assignee_user_id: Optional[str] = None,
                             team_id: Optional[str] = None) -> Dict[str, Any]:
        """Propose an edit the person approves, seeing the exact text, and which then APPLIES.

        For the case where a run reads someone's plan and finds a field that no longer describes
        reality -- a stale ``current_state``, a goal whose period has slipped, a habit scheduled on
        days they stopped using. The run has the better value; it must not simply write it, because
        a field like ``current_state`` is the person's own account of where they are.

        ``changes`` is a list of ``{"field", "current", "proposed"}``. The summary is built to be
        READ: what is changing, in human words, with the current and proposed values in full, so
        the approval card shows the actual text rather than a description of it. Approving applies
        it directly, with no second AI run.

        Which fields are allowed, and what each is called, is the SERVER's registry
        (``decision_field_edit.FIELD_SPECS``) -- a payload naming an unregistered field is rejected
        at creation, here, rather than at approval time.
        """
        lines = [f"Proposed change to your {target_type}."]
        if why:
            lines.append("")
            lines.append(why)
        for change in changes:
            field = str(change.get("field") or "")
            lines.append("")
            lines.append(f"--- {field} ---")
            lines.append("CURRENT:")
            lines.append(str(change.get("current") or "(empty)"))
            lines.append("")
            lines.append("PROPOSED:")
            lines.append(str(change.get("proposed") or ""))
        return self.create_decision(
            "\n".join(lines), kind="approve", quest_id=quest_id,
            assignee_user_id=assignee_user_id, team_id=team_id,
            executable={"kind": "field_edit",
                        "target": {"type": target_type, "id": target_id},
                        "changes": list(changes)})

    def assignee_id(self, assignee: Optional[str] = None) -> str:
        """Resolve a configured ROLE NAME (``"owner"``, ``"operator"``, ...) to a user id.

        Names come from ``decision_assignees`` (RunnerConfig / ``QAR_DECISION_ASSIGNEES``).
        ``None`` yields ``default_assignee_user_id``. An unknown name raises rather than silently
        routing an approval to the default person — sending a payment approval to the wrong human
        because a role was misspelled is exactly the failure this lookup exists to prevent.
        """
        if assignee is None:
            return self.default_assignee_user_id
        key = str(assignee).strip()
        if key in self.decision_assignees:
            return self.decision_assignees[key]
        known = ", ".join(sorted(self.decision_assignees)) or "(none configured)"
        raise QuestNotConfigured(
            f"unknown decision assignee {key!r}; configured roles: {known}. Add it to "
            f"decision_assignees in your config file (or QAR_DECISION_ASSIGNEES).")

    def create_decision(self, summary: str, *, kind: str = "approve",
                        quest_id: Optional[str] = None, assignee_user_id: Optional[str] = None,
                        assignee: Optional[str] = None,
                        default_on_silence: str = "hold",
                        team_id: Optional[str] = None,
                        deadline: Optional[datetime] = None,
                        executable: Optional[Dict[str, Any]] = None,
                        review_url: Optional[str] = None) -> Dict[str, Any]:
        # ``assignee`` names a ROLE from config; ``assignee_user_id`` is the raw id. An explicit id
        # wins, so an existing caller is unaffected. Resolution happens OUTSIDE the try/except
        # below on purpose: a misspelled role must raise, not degrade into an unassigned decision.
        if assignee is not None and not assignee_user_id:
            assignee_user_id = self.assignee_id(assignee)
        elif assignee_user_id is None and self.default_assignee_user_id:
            assignee_user_id = self.default_assignee_user_id
        if kind == EXPERIMENT_PROPOSAL_KIND and quest_id:
            # At most ONE open experiment proposal per quest, enforced here where it is created so
            # it holds for every caller (several characters on one quest, a retry, a race). The
            # existing row is returned as-is; the person already has the question in front of them.
            # A failed read of the open decisions lets the create through: a duplicate is the lesser
            # harm next to a proposal that can never be raised.
            for open_decision in self.list_open_decisions_for_quest(quest_id):
                if str(open_decision.get("kind") or "") == EXPERIMENT_PROPOSAL_KIND:
                    log.info("create_decision: quest %s already has an open experiment proposal; "
                             "not raising another", quest_id)
                    return open_decision
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
            # ``kind`` must satisfy Quest's ``^[a-z0-9_:-]{1,64}$`` or the POST 422s. A caller that
            # built one from free-form planner output (or a hallucinated value) degrades to the
            # generic "approve" here rather than losing the whole escalation to a rejected request.
            if not isinstance(kind, str) or not DECISION_KIND_RE.match(kind):
                log.warning("create_decision: kind %r does not match Quest's ^[a-z0-9_:-]+$ "
                           "(max 64 chars); using 'approve' instead", kind)
                kind = "approve"
            body: Dict[str, Any] = {"kind": kind, "summary": summary,
                                    "default_on_silence": default_on_silence}
            if quest_id:
                body["quest_id"] = quest_id
            if assignee_user_id:
                body["assigned_to_user_id"] = assignee_user_id
            if deadline is not None:
                # Accept either a real datetime (the normal case) or an already-ISO string, so a
                # caller that pre-formatted one for some other reason is not rejected here.
                body["deadline"] = deadline.isoformat() if hasattr(deadline, "isoformat") else str(deadline)
            if review_url and re.match(r"^https?://\S+$", str(review_url)):
                # The page the person should open to decide (draft / preview / doc / PR); Quest
                # renders it as a prominent link on the ask. Dropped, not rejected, if malformed.
                body["review_url"] = str(review_url)
            if executable:
                # An action to APPLY on approval. The server accepts only structured, data-shaped
                # kinds here (see propose_field_change) and validates the payload at creation, so a
                # proposal that could never apply is rejected now rather than becoming an ask the
                # person answers into nothing.
                body["executable"] = dict(executable)
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

    def list_decisions_for_quest(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET every decision (open AND resolved) that references a specific quest.

        The backend route is ``GET /api/teams/decisions/for-quest?quest_id=`` (NOT a team-scoped
        path: it is quest-scoped and derives access from the quest). It returns a BARE LIST with
        no status filter available server-side -- narrowing by ``status`` is left to the caller
        (``list_open_decisions_for_quest`` below does the open-only case; a context builder wants
        both open and resolved). Each item may carry ``resolution``, ``response_text``,
        ``resolved_by_name``, ``auto_resolved``, ``overdue`` when resolved -- treat all of these as
        optional, since not every deployment's backend fills every field.

        Best-effort: returns ``[]`` on any failure (unconfigured client, network, a backend
        without the route) so a caller can never be aborted by this lookup.
        """
        try:
            resp = self._request("GET", "/api/teams/decisions/for-quest",
                                 params={"quest_id": quest_id})
            return resp if isinstance(resp, list) else list((resp or {}).get("decisions") or [])
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_decisions_for_quest failed for quest %s: %s", quest_id, e)
            return []

    def list_open_decisions_for_quest(self, quest_id: str) -> List[Dict[str, Any]]:
        """The OPEN (unresolved) subset of ``list_decisions_for_quest``.

        Used by Autopilot's HOLD gate (see ``runner.autopilot``): a quest with an open human
        decision already pending should not have more autonomous work piled onto it. Filtered on
        ``status == "open"`` (``TeamDecisionStatus.OPEN``; the other values are ``resolved`` /
        ``expired``). Best-effort: ``[]`` on any failure, same as the call it wraps. The CALLER
        logs the skip reason either way, so a quest is never silently passed over.
        """
        return [d for d in self.list_decisions_for_quest(quest_id)
                if str(d.get("status") or "").strip().lower() == "open"]

    def list_decisions_for_quest(self, quest_id: str, *,
                                 status: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/teams/decisions/for-quest?quest_id=... — ALL of a quest's decisions.

        Unlike ``list_open_decisions_for_quest`` (which the autopilot HOLD gate uses and which
        filters to open only), this returns open AND resolved rows by default -- the quest's full
        decision history. Pass ``status`` to filter client-side (the backend has none) to one value
        (e.g. "resolved"). Returns [] on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/teams/decisions/for-quest",
                                 params={"quest_id": quest_id})
            rows = resp if isinstance(resp, list) else list((resp or {}).get("decisions") or [])
            if status:
                rows = [d for d in rows
                        if str(d.get("status") or "").strip().lower() == status.lower()]
            return rows
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_decisions_for_quest failed for quest %s: %s", quest_id, e)
            return []

    def list_asks(self, *, quest_id: str, limit: int = 50) -> Optional[List[Dict[str, Any]]]:
        """GET /api/asks?quest_id=... -- every tracked ask on this quest, for Autopilot's
        REACTIVE-mode gate (see ``autopilot.has_new_ask_since``): a quest in that mode makes its
        scheduled pass only when something has actually been asked of it since the last one.

        ``mine_only=false`` on purpose: the gate needs to see EVERY ask on the quest, not only
        ones the calling account itself filed, since a request usually comes from the quest's
        owner or a teammate, not from Autopilot's own account.

        Returns ``[]`` when the read succeeded and there is genuinely nothing there (including a
        malformed/empty response body -- the backend answered, so there is nothing to fail open
        over). Returns ``None`` on ``QuestApiError``/``QuestNotConfigured`` -- a failed READ, kept
        distinguishable from a successful read of nothing so the REACTIVE gate can fail OPEN
        (run the pass) instead of reading the failure as "no new ask since the last pass". Never
        raises, matching ``list_tasks``/``list_quests``.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/asks",
                                 params={"quest_id": quest_id, "mine_only": "false",
                                         "limit": limit}) or {}
            items = resp.get("items") if isinstance(resp, dict) else None
            return items if isinstance(items, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_asks failed for quest %s: %s", quest_id, e)
            return None

    def patch_ask(self, item_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH /api/asks/{item_key} with ``kind`` and/or ``assignee_id`` (an empty string clears
        the assignee). Raises ``QuestApiError`` on refusal; the caller decides whether to retry."""
        return self._request("PATCH", f"/api/asks/{item_key}", body=fields) or {}

    def record_ask(self, quest_id: str, text: str, *, author_id: Optional[str] = None,
                   kind: Optional[str] = None, source: str = "ai_recorded",
                   item_id: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/asks: file a request a person made of the AI on this quest (What You Asked).

        ``text`` is the person's exact words; ``author_id`` the person who asked (their email,
        default: the account this client runs as); ``kind`` is ``request`` (the server's default),
        ``standing``, ``context`` or ``question``. Recording the same words twice is the same ask
        (the server derives the id from them unless ``item_id`` is given), so a retry is safe.
        Returns the ask row (its ``key`` is what ``update_ask`` takes), or ``{}`` on any failure
        (logged), so a caller can tell the person was not heard rather than assume they were.
        """
        body: Dict[str, Any] = {"quest_id": quest_id, "text": text, "source": source}
        if author_id:
            body["author_id"] = author_id
        if kind:
            body["kind"] = kind
        if item_id:
            body["item_id"] = item_id
        try:
            self._require()
            return self._request("POST", "/api/asks", body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("record_ask failed for quest %s: %s", quest_id, e)
            return {}

    def update_ask(self, item_key: str, state: Optional[str] = None, note: str = "", *,
                   kind: Optional[str] = None) -> Dict[str, Any]:
        """PATCH /api/asks/{item_key} as a RUN's claim (``by_run``), never a person's decision.

        ``state`` is one of in progress, done, blocked, declined, noted, awaiting answer
        (underscores are accepted): ``done`` lands as "awaiting acceptance" until a person accepts
        it, and ``accepted`` is refused. ``kind`` (request, standing, context, question) sorts an
        ask without changing its state, and never overrides a kind a person set. Pass a state, a
        kind, or both. Returns the updated ask row, or ``{}`` on any failure (logged).
        """
        body: Dict[str, Any] = {"by_run": True}
        if state:
            body["state"] = state
        if kind:
            body["kind"] = kind
        if note:
            body["note"] = note
        try:
            self._require()
            return self._request("PATCH", f"/api/asks/{item_key}", body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("update_ask failed for ask %s: %s", item_key, e)
            return {}
