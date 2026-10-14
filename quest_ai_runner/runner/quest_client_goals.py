"""Goals and plans: listing, editing, notes, updates, day plans and the owning-team lookup.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .quest_client_support import (
    GOAL_UPDATE_FANOUT_CAP,
    QuestApiError,
    QuestNotConfigured,
    _PERIOD_RE,
    log,
)


class GoalsMixin:
    """Goals and plans: listing, editing, notes, updates, day plans and the owning-team lookup."""

    def owning_team_for(self, quest_id: str) -> str:
        """The team that actually owns ``quest_id``, per the owner-scoped quest list.

        A lane's configured ``team_id`` is not always the quest's team: an owner-scoped lane
        legitimately syncs quests across every team its owner belongs to, and a quest can be moved
        between teams at any time. Cached per quest, and only ever consulted after a team-scoped
        call has already failed, so the ordinary path costs nothing. Returns "" when unknown.

        Only a FOUND team is cached. A miss (the quest is not in the owner's list, or the list
        itself failed) is answered fresh next time: caching "" would turn one transient listing
        failure into a quest that never resolves for the life of the process. Callers that learn
        the cached team is wrong call ``forget_owning_team`` and ask again.
        """
        cached = self._quest_team_cache.get(quest_id)
        if cached is not None:
            return cached
        found = ""
        for q in self.list_my_quests():
            qid = str(q.get("quest_id") or q.get("id") or "")
            if qid == quest_id:
                found = str(q.get("team_id") or "")
                break
        if found:
            self._quest_team_cache[quest_id] = found
        return found

    def forget_owning_team(self, quest_id: str) -> bool:
        """Drop the cached owning team for ``quest_id``; True if there was one.

        The cache has no TTL and nothing tells the runner when a quest changes team, so the only
        signal that an entry is stale is the cached team failing to serve the quest. The caller
        that sees that failure forgets the entry and resolves once more.
        """
        return self._quest_team_cache.pop(quest_id, None) is not None

    def goals_from_owning_team(self, quest_id: str, failed_team: str,
                                err: QuestApiError, *, resolved_now: bool) -> Dict[str, Any]:
        """Retry a failed team-scoped goals read on the team that actually owns the quest.

        Two rounds at most. The first uses the cached resolution (or resolves fresh when nothing
        is cached). If that team also fails, or is the team that already failed, the cache is
        stale: the quest moved again since it was filled. Forget it and resolve ONCE more against
        the live quest list. ``resolved_now`` says the cache was filled during this very call, in
        which case a second listing would only repeat the first and is skipped. Re-raises the last
        failure when no team serves the quest.
        """
        tried = {failed_team}
        have_cached = quest_id in self._quest_team_cache and not resolved_now
        for round_ in ("cached", "fresh"):
            if round_ == "fresh":
                if not have_cached:
                    break  # the previous round already looked at the live list
                self.forget_owning_team(quest_id)
                log.info("quest %s is no longer served by its cached team; re-resolving", quest_id)
            owner_tid = self.owning_team_for(quest_id)
            if not owner_tid or owner_tid in tried:
                continue
            log.info("quest %s is not on team %s; using its own team %s",
                     quest_id, failed_team, owner_tid)
            try:
                return self._request(
                    "GET", f"/api/teams/{owner_tid}/quests/{quest_id}/goals") or {}
            except QuestApiError as e:
                tried.add(owner_tid)
                err = e
        raise err

    def list_quest_goals(self, quest_id: str, *,
                         team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals — goals grouped by time period.

        Returns {quest_id, outcome, period_groups: [{time_scope, period, period_label, goals: [{id,
        name, time_scope, period, period_label, deadline, completed, parent_goal_id}]}]}.
        Groups are ordered year → quarter → month → week → day → custom, then chronologically.

        The endpoint is team-scoped but the quest need not be on the CLIENT'S team: an owner-scoped
        lane syncs quests across all of its owner's teams, and a quest can be moved to another team
        at any time, after which the configured team 404s that quest forever. So when the caller
        did not name a team and the configured one does not own the quest, resolve the quest's own
        team (cached) and retry there; if the cached team has ALSO stopped serving the quest, it
        moved again, so the stale entry is dropped and resolved once more from the live quest
        list. An explicit ``team_id`` is honored as given and never second-guessed.

        CACHE-FIRST (a lane serving SEVERAL teams): when the caller named no team and this quest's
        owning team is already known, that team is used straight away rather than after the
        configured team has failed. On a single-team lane the cache is empty for every quest it
        reads, so this costs nothing and changes nothing. The cache is consulted, never FILLED,
        here: filling it means a quest-list call, and paying for one on every uncached quest --
        including on lanes where the configured team was always going to serve it -- would turn a
        multi-team convenience into a per-quest tax on everybody. The failure path below fills it.
        """
        try:
            self._require()
            tid = team_id or self._quest_team_cache.get(quest_id) or self.team_id
            resolved_now = False
            if not tid and not team_id:
                resolved_now = quest_id not in self._quest_team_cache
                tid = self.owning_team_for(quest_id)
            if not tid:
                raise QuestNotConfigured("team_id is required to list quest goals")
            try:
                return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}/goals") or {}
            except QuestApiError as e:
                if team_id:
                    raise  # caller named the team: their call, their error
                return self.goals_from_owning_team(
                    quest_id, tid, e, resolved_now=resolved_now)
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_goals failed for quest %s: %s", quest_id, e)
            return {}

    def update_goal(self, goal_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /api/planning/goals/{goal_id} — edit one goal's own fields.

        The endpoint accepts ``title``/``name``, ``description``, ``criteria``, ``period``,
        ``timeScope``, ``deadline``-adjacent scheduling fields and the assignee/aiHelp pair. Send
        only what changed; anything omitted keeps its value.

        Returns {} on failure rather than raising: a goal edit is one item in a sync that has
        others to finish, and one rejected rename must not abort the rest.
        """
        try:
            return self._request("PUT", f"/api/planning/goals/{goal_id}", body=dict(fields)) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("update_goal failed for %s: %s", goal_id, e)
            return {}

    def set_goal_completed(self, goal_id: str, *, completed: bool = True) -> Dict[str, Any]:
        """POST /api/planning/goals/{goal_id}/complete — tick or untick one goal.

        Separate from ``update_goal`` because completion is its own endpoint server-side, and
        because it is the one goal edit that changes what the plan MEANS rather than how it reads.
        """
        try:
            return self._request(
                "POST", f"/api/planning/goals/{goal_id}/complete",
                body={"goalId": goal_id, "completed": bool(completed)}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("set_goal_completed failed for %s: %s", goal_id, e)
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
                    assignee_rep_id: Optional[str] = None,
                    assigned_to_user_id: Optional[str] = None) -> Dict[str, Any]:
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

        ``assigned_to_user_id`` assigns the goal, at creation, to a specific HUMAN member of
        ``quest_id`` (a Quest user_id, not an AI persona -- see ``assignee_rep_id`` for that).
        The backend authorizes this the same way as its separate assign-after-creation endpoint:
        the caller must be able to edit the quest, and the assignee must already be a member of
        it, or the request is refused with no goal created. Requires ``quest_id`` -- a standalone
        goal has no quest membership to check the assignee against.

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
        if assigned_to_user_id is not None:
            body["assigned_to_user_id"] = assigned_to_user_id
        return self._request("POST", "/api/planning/goals", body=body) or {}

    def list_assignable_members(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET /api/planning/quests/{quest_id}/assignable-members: the humans a goal on this quest
        can be assigned to (``[{user_id, name, email, is_owner}]``). ``[]`` on any failure."""
        try:
            resp = self._request(
                "GET", f"/api/planning/quests/{quest_id}/assignable-members") or {}
            members = resp.get("members") if isinstance(resp, dict) else None
            return members if isinstance(members, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_assignable_members failed for quest %s: %s", quest_id, e)
            return []

    def set_goal_ai_handling(self, goal_id: str, handling: str, *,
                             rep_id: Optional[str] = None,
                             decided_by: str = "rep") -> Dict[str, Any]:
        """PUT /api/planning/goals/{goal_id}/ai-handling: hand a human-assigned goal to the
        assignee's AI rep (``handling="rep"``) or keep it with the person (``"me"``). The backend
        applies the assignee's own preference (auto, ask, never) and may answer ``proposed``."""
        body: Dict[str, Any] = {"handling": handling, "decided_by": decided_by}
        if rep_id is not None:
            body["rep_id"] = rep_id
        return self._request("PUT", f"/api/planning/goals/{goal_id}/ai-handling", body=body) or {}

    def get_goal(self, goal_id: str, *, quest_id: Optional[str] = None,
                 team_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch one goal's metadata: id, name, description, criteria, deadline, completed, ...

        Attempts ``GET /api/teams/{team_id}/quests/{quest_id}/goals/{goal_id}`` first, in case
        some deployment serves it -- but THE REFERENCE BACKEND HAS NO SINGLE-GOAL ROUTE (verified
        live: this call 404s on every call today). The real path, and the one that actually runs
        in practice, is scanning ``list_quest_goals(quest_id)`` (the team's goals grouped by
        period) for the matching id. That scan is where ``description`` (the goal's brief) comes
        from, along with ``criteria``/``deadline``/``completed``/``parent_goal_id``.

        Requires ``quest_id`` -- there is nothing to scan without it -- either as a parameter or
        implied by ``list_quest_goals``' own team resolution; ``team_id`` is optional (only used
        for the direct attempt, which the scan does not need). Returns {} when ``quest_id`` is
        omitted, when the goal is genuinely not on that quest, or on any failure.
        """
        try:
            self._require()
            if not quest_id:
                return {}
            tid = team_id or self.team_id
            if tid:
                try:
                    direct = self._request(
                        "GET", f"/api/teams/{tid}/quests/{quest_id}/goals/{goal_id}")
                    if direct:
                        return direct
                except QuestApiError as e:
                    log.info("get_goal direct route unavailable for goal %s (%s); scanning "
                             "list_quest_goals instead", goal_id, e)
            for group in (self.list_quest_goals(quest_id, team_id=team_id).get("period_groups") or []):
                for goal in (group.get("goals") or []):
                    if str(goal.get("id")) == str(goal_id):
                        return goal
            return {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_goal failed for goal %s: %s", goal_id, e)
            return {}

    def list_goal_notes(self, goal_id: str, *, quest_id: Optional[str] = None,
                        team_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/quests/{quest_id}/goals/{goal_id}/notes — fetch recent notes.

        LEGACY ROUTE: the reference backend has no ``.../goals/{goal_id}/notes`` endpoint either
        (verified live, same situation as ``get_goal``'s single-goal route). The per-goal thread
        it actually serves is ``goal_updates`` (see ``list_goal_updates``) -- new code should call
        that directly instead of this method. This method still attempts the legacy route first
        (in case some deployment serves it), and on failure falls back to ``list_goal_updates``,
        mapping each update to the note-ish shape callers here expect: ``{"id": updateId, "text":
        note, "author_name": userName, "created_at": createdAt}``. A goal update carries no
        ``author_kind`` field, so none is invented on the mapped dict.

        Returns [] if not found, no notes/updates, or on any failure.
        """
        tid = team_id or self.team_id
        if tid and quest_id:
            try:
                self._require()
                resp = self._request(
                    "GET",
                    f"/api/teams/{tid}/quests/{quest_id}/goals/{goal_id}/notes",
                    params={"limit": limit}
                )
                return (list(resp.get("notes") or resp or []) if isinstance(resp, dict)
                        else (list(resp) if isinstance(resp, list) else []))
            except (QuestApiError, QuestNotConfigured) as e:
                log.info("list_goal_notes legacy route unavailable for goal %s (%s); falling "
                         "back to list_goal_updates", goal_id, e)
        updates = self.list_goal_updates(goal_id, limit=limit)
        return [
            {
                "id": u.get("updateId"),
                "text": u.get("note"),
                "author_name": u.get("userName"),
                "created_at": u.get("createdAt"),
            }
            for u in updates
        ]

    # --- goal updates (the per-goal check-in thread) --------------------------
    # A "goal update" is the per-goal check-in thread in the Quest product: the place a person
    # (or this runner) writes what they did / found on ONE goal. See list_goal_notes above for
    # why these, not "notes", are the real per-goal thread on the reference backend.

    def list_goal_updates(self, goal_id: str, *, limit: int = 20,
                          before: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/planning/goals/{goal_id}/updates?limit=&before= — a goal's check-in thread.

        Newest first. ``before`` pages backwards with an ISO timestamp (a prior update's
        ``createdAt``); the server defaults to limit 20, caps at 100. Each item is ``{updateId,
        goalId, userId, userName, note, shared, shareTarget, shareRefId, createdAt}``.

        Accepts both the ``{"updates": [...]}`` envelope and a bare list (same tolerance as
        ``discover_due``), so this keeps working against a mock or a future shape change. Returns
        [] on any failure (unconfigured client, network, backend without the route).
        """
        try:
            self._require()
            params: Dict[str, Any] = {"limit": limit}
            if before:
                params["before"] = before
            resp = self._request(
                "GET", f"/api/planning/goals/{goal_id}/updates", params=params)
            if isinstance(resp, dict):
                return list(resp.get("updates") or [])
            return list(resp) if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_goal_updates failed for goal %s: %s", goal_id, e)
            return []

    def add_goal_update(self, goal_id: str, note: str, *, shared: bool = False) -> Dict[str, Any]:
        """POST /api/planning/goals/{goal_id}/updates {"note", "shared"} — write a check-in.

        ``note`` must be 1..2000 non-blank characters; the server rejects a blank note (422/400)
        or one over 2000 chars, not validated client-side here so the caller sees the real
        backend error. ``shared`` marks the update visible beyond the goal owner (``shareTarget``/
        ``shareRefId`` are server-set, not settable here). Returns the created update dict (same
        wire shape as ``list_goal_updates``), or {} on failure.
        """
        try:
            return self._request(
                "POST", f"/api/planning/goals/{goal_id}/updates",
                body={"note": note, "shared": bool(shared)}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_goal_update failed for goal %s: %s", goal_id, e)
            return {}

    def delete_goal_update(self, goal_id: str, update_id: str) -> bool:
        """DELETE /api/planning/goals/{goal_id}/updates/{update_id} — soft-delete one check-in.

        Allowed for the update's own author or an admin of the goal's quest (server-enforced).
        Returns True only on success; False on any failure (unconfigured client, network, not
        permitted, already deleted).
        """
        try:
            self._request("DELETE", f"/api/planning/goals/{goal_id}/updates/{update_id}")
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("delete_goal_update failed for goal %s update %s: %s",
                       goal_id, update_id, e)
            return False

    def list_quest_goal_updates(self, quest_id: str, *, limit_per_goal: int = 20,
                                limit: int = 200,
                                goal_ids: Optional[List[str]] = None
                                ) -> Dict[str, List[Dict[str, Any]]]:
        """Every goal update on a quest, grouped ``{goal_id: [update, ...]}``.

        Saves a caller that wants a whole quest's check-ins from writing the fan-out itself. Two
        paths, tried in order:

        1. THE BULK PATH: one call to ``GET /api/planning/quests/{quest_id}/goal-updates?
           limit_per_goal=<n>`` -- a route being added to the reference backend in parallel with
           this client. It answers ``{"updates": [...]}`` with the SAME wire shape as
           ``list_goal_updates``, each row carrying its own ``goalId``; this method groups them
           by that field.
        2. THE FALLBACK: the bulk route fails for ANY reason (a backend that does not have it yet
           answers 404). Resolve the quest's goal ids -- via ``list_quest_goals(quest_id)`` (all
           period groups flattened), or the caller's own ``goal_ids`` when given -- and call
           ``list_goal_updates`` once per goal. A goal with no updates (or that itself fails) is
           skipped rather than included as an empty list, so the result only ever names goals
           that actually have something.

        ``limit`` bounds the BULK path's total rows, and is sent explicitly because the two caps
        compose: the server applies its overall limit FIRST and trims per goal afterwards, so
        leaving it at the server default (50) would silently drop the oldest goals' check-ins on a
        quest with many goals, whatever ``limit_per_goal`` said. 200 is the server's own maximum.
        The fan-out path asks each goal for ``limit_per_goal`` directly, over at most
        ``GOAL_UPDATE_FANOUT_CAP`` goals, and stops early if the backend rate-limits it.

        Returns {} when both paths come up empty (no goals, or every call failed).
        """
        try:
            resp = self._request(
                "GET", f"/api/planning/quests/{quest_id}/goal-updates",
                params={"limit_per_goal": limit_per_goal, "limit": limit})
            rows = resp.get("updates") if isinstance(resp, dict) else resp
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for row in (rows or []):
                gid = str(row.get("goalId") or "")
                if gid:
                    grouped.setdefault(gid, []).append(row)
            return grouped
        except (QuestApiError, QuestNotConfigured) as e:
            log.info("list_quest_goal_updates bulk route unavailable for quest %s (%s); "
                     "falling back to per-goal fan-out", quest_id, e)

        ids = list(goal_ids) if goal_ids else [
            str(goal.get("id") or "")
            for group in (self.list_quest_goals(quest_id).get("period_groups") or [])
            for goal in (group.get("goals") or [])
        ]
        ids = [gid for gid in ids if gid]
        if len(ids) > GOAL_UPDATE_FANOUT_CAP:
            log.warning(
                "list_quest_goal_updates: quest %s has %d goals and this backend has no bulk "
                "goal-updates route, so the per-goal fan-out is capped at %d; %d goal(s) go "
                "unread this pass rather than spending the whole rate-limit budget here",
                quest_id, len(ids), GOAL_UPDATE_FANOUT_CAP, len(ids) - GOAL_UPDATE_FANOUT_CAP)
            ids = ids[:GOAL_UPDATE_FANOUT_CAP]
        grouped = {}
        for gid in ids:
            # Called through ``_request`` rather than ``list_goal_updates`` ON PURPOSE: that
            # method swallows every failure into ``[]``, so a rate-limited fan-out kept firing
            # the remaining calls and kept being refused. Here a 429 STOPS the fan-out, which is
            # what a backend saying "slow down" is asking for.
            try:
                resp = self._request(
                    "GET", f"/api/planning/goals/{gid}/updates",
                    params={"limit": limit_per_goal})
            except (QuestApiError, QuestNotConfigured) as e:
                if getattr(e, "status", None) == 429:
                    log.warning(
                        "list_quest_goal_updates: rate limited partway through quest %s's "
                        "per-goal fan-out (%d of %d goals read); stopping here so the rest of "
                        "this run keeps its API budget", quest_id, len(grouped), len(ids))
                    break
                log.warning("list_quest_goal_updates: goal %s failed: %s", gid, e)
                continue
            updates = (list(resp.get("updates") or []) if isinstance(resp, dict)
                       else list(resp) if isinstance(resp, list) else [])
            if updates:
                grouped[gid] = updates
        return grouped

    # --- planning: goals/day-plan/habits (generic Quest planning surface) ------------------------

    def list_current_goals(self) -> Dict[str, Any]:
        """GET /api/planning/goals/current/all — every quest's CURRENT-period goals, grouped by quest.

        Returns ``{"quests": [{"quest_id", "goals": [...], "current_periods": {...}}]}``. Each goal
        carries ``id, name, completed, deadline, timeScope (day|week|month|quarter|year), period,
        periodLabel, slipCount, aiHelp, assigneeRepId, description, criteria``. Contains NO day-scope
        goals -- use ``get_day_plan`` for "what's on today". Returns {} on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/planning/goals/current/all") or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_current_goals failed: %s", e)
            return {}

    def get_day_plan(self, *, date: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/daily-plan/yesterday-goals[?for_date=] — one day's actual goal/habit plan.

        Despite the route's name, ``for_date`` returns THAT date's plan; omitting it returns
        YESTERDAY's, not today's -- pass today's own date explicitly for "what's on today". This is
        a different, richer surface than ``get_daily_reflection`` (which reads the written daily-plan
        REFLECTION, not the goal list): returns ``{date, total, completed_count, goals: [...],
        habit_stats: {...}}``, each goal carrying ``id, name, completed, quest_id, quest_name,
        scheduled_time, is_carried_over``. Returns {} on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/daily-plan/yesterday-goals",
                                 params={"for_date": date} if date else None) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_day_plan failed (date=%s): %s", date, e)
            return {}

    def list_all_plans(self) -> List[Dict[str, Any]]:
        """GET /api/planning/plans/all — every goal across every quest and period (a full sweep).

        For a full-portfolio review, not a daily/weekly question (use ``list_current_goals`` or
        ``get_day_plan`` for those). Each item carries ``title`` and ``abandoned`` in addition to the
        fields ``list_current_goals`` returns. Returns [] on any error or unexpected shape.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/planning/plans/all")
            if isinstance(resp, list):
                return resp
            items = (resp.get("plans") or resp.get("items")) if isinstance(resp, dict) else None
            return items if isinstance(items, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_all_plans failed: %s", e)
            return []

    def carry_over_day_plan(self) -> Dict[str, Any]:
        """POST /api/daily-plan/carry-over — roll yesterday's unfinished goals into today's plan.

        Returns the response dict. Returns {} on any error.
        """
        try:
            self._require()
            return self._request("POST", "/api/daily-plan/carry-over") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("carry_over_day_plan failed: %s", e)
            return {}

    def delete_goal(self, goal_id: str) -> bool:
        """DELETE /api/planning/goals/{goal_id} — permanently remove one goal.

        Returns True only when the delete succeeded. Irreversible: a caller acting on a person's
        behalf must have their explicit go-ahead before calling this.
        """
        try:
            self._require()
            self._request("DELETE", f"/api/planning/goals/{goal_id}")
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("delete_goal failed for %s: %s", goal_id, e)
            return False
