"""Quests: teams, fields, lifecycle, notes, events, habits, experiments and autopilot settings.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .quest_client_support import QuestApiError, QuestNotConfigured, QuestPendingApproval, log

#: Newest notes a pull fetches by default, and the page size it asks the server for (the server
#: caps a page at 100 whatever is asked).
DEFAULT_NOTES_LIMIT = 200
NOTES_PAGE_SIZE = 100


class NotesList(list):
    """A list of notes (oldest -> newest) that also knows how many the quest has in all.

    Behaves exactly like the plain list ``list_quest_notes`` always returned; ``total`` lets a
    renderer say "N older notes not shown, total T" when the list is the newest slice.
    """

    def __init__(self, items=(), total: Optional[int] = None):
        super().__init__(items)
        self.total = len(self) if total is None else total



class QuestsMixin:
    """Quests: teams, fields, lifecycle, notes, events, habits, experiments and autopilot settings."""

    # --- quest and goal browsing (for interactive chat context selection) ------

    def list_teams(self) -> List[Dict[str, Any]]:
        """GET /api/teams — every team the authenticated account is a member of.

        This is the org-level view: a deployment's app account usually sits on all of its
        organization's teams, so walking these and calling ``list_quests(team_id=...)`` on each
        answers "which quests can you act on" without assuming the configured ``team_id`` is the
        whole picture. Returns [] on any failure.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/teams") or []
            if isinstance(resp, dict):
                resp = resp.get("teams") or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_teams failed: %s", e)
            return []

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

    def edit_quest_field(self, quest_id: str, fields: Dict[str, Any], *,
                         actor: str = "ai", user_requested: bool = False) -> Dict[str, Any]:
        """PATCH /api/quests/{quest_id}/field — owner-scoped edit of AI-determined quest fields.

        The OTHER write path, and the one to reach for when a field the team route refuses needs
        changing: this one is scoped to the quest's owner (or an active share) rather than to a
        team role, and it covers the fields the AI itself sets -- ``outcome``, ``current_state``,
        ``preferences``, ``strategies``, ``purpose`` (there is no ``purpose`` field on
        ``start_quest`` -- set it here after creating the quest). ``current_state`` in particular
        is writable HERE and nowhere in ``write_quest_fields``' role scopes, which is easy to
        mistake for "nothing can write it".

        The backend route only accepts ONE field per call (a legacy ``{"field_name", "value"}``
        body, not a batch -- an earlier version of this method sent ``{"fields": {...}}``, which
        the handler silently never read, so every call through it wrote nothing). This method
        sends one PATCH per entry in ``fields`` and returns the LAST response (each individual
        PATCH returns the full updated quest). Returns {} the moment any field fails to write, so
        a partial failure is never reported as a full success.

        Prefer ``write_quest_fields`` when a team ROLE should govern the write (an AI service
        member must not silently rewrite intent). Use this when the caller is acting as the owner.

        ``actor`` DECLARES WHO DECIDED THE VALUE, and defaults to ``"ai"`` because that is what a
        runner is. These fields are the person's own account of their quest, so a backend that
        supports the declaration may refuse to apply an AI-decided value and answer 202 with a
        ``decision_id`` instead: the change becomes an ask the owner approves, rather than a
        rewrite they find later. ``user_requested=True`` says the user's own message explicitly
        asked for THIS field to change, which is a structured verdict the caller must already
        hold; never derive it by reading words out of model output. Pass ``actor="human"`` only
        when a person literally supplied this value. A backend that does not know the field
        ignores it and behaves exactly as before.
        """
        result: Dict[str, Any] = {}
        for field_name, value in fields.items():
            try:
                result = self._request(
                    "PATCH", f"/api/quests/{quest_id}/field",
                    body={"field_name": field_name, "value": value,
                          "actor": actor, "userRequested": bool(user_requested)}) or {}
            except (QuestApiError, QuestNotConfigured) as e:
                log.warning("edit_quest_field failed for quest %s field %s: %s",
                           quest_id, field_name, e)
                return {}
            if isinstance(result, dict) and result.get("applied") is False:
                log.info("edit_quest_field: %s on quest %s was not applied (%s); it is an ask for "
                         "the owner to approve (decision %s)",
                         field_name, quest_id, result.get("reason"), result.get("decision_id"))
                return result
        return result

    def start_quest(self, *, category_id: str,
                    outcome: Optional[str] = None,
                    acceptance_criteria: Optional[str] = None,
                    current_state: Optional[str] = None,
                    subcategory_id: Optional[str] = None,
                    linked_domain_ids: Optional[List[str]] = None,
                    linked_collection_ids: Optional[List[str]] = None,
                    preferences: Optional[str] = None,
                    timeline_days: Optional[int] = None,
                    start_date: Optional[str] = None,
                    category_ids: Optional[List[str]] = None,
                    creation_mode: str = "quick") -> Dict[str, Any]:
        """POST /api/quests/start — create a brand-new top-level Quest under the caller's account.

        ``category_id`` is required (a string like "cat_career"; no server-side enum enforced at
        time of writing). Passing a non-empty ``outcome`` marks the quest ``is_quick_quest =
        True``, which sets ``setup_complete = True`` immediately and leaves ``strategies``/
        ``plan`` empty -- this is the way to create a quest with just context, no
        strategy-generation or plan-generation phase. ``preferences`` is a single string (becomes
        ``preferences.beliefs[0]`` server-side, NOT the same thing as the quest's ``purpose``
        field -- there is no ``purpose`` field on this endpoint at all; set it afterward with
        ``edit_quest_field(quest_id, {"purpose": ...})``). ``creation_mode`` is "quick" or
        "ai_assisted" (subscription-gated); "quick" is the literal match for "just create it, no
        AI-assisted flow".

        The created quest is account-wide (no team), not yet on any team's board -- pair with
        ``attach_quest_to_team`` to put it on one. Raises ``QuestApiError``/``QuestNotConfigured``
        on failure rather than swallowing it: a caller that reports "quest created" must know it
        was. When Quest holds the creation for a person's approval (an AI account's request),
        ``QuestPendingApproval`` is raised with the ask's ``decision_id``: the quest does not exist yet.
        """
        self._require()
        body: Dict[str, Any] = {"category_id": category_id, "creation_mode": creation_mode}
        if outcome is not None:
            body["outcome"] = outcome
        if acceptance_criteria is not None:
            body["acceptance_criteria"] = acceptance_criteria
        if current_state is not None:
            body["current_state"] = current_state
        if subcategory_id is not None:
            body["subcategory_id"] = subcategory_id
        if linked_domain_ids is not None:
            body["linked_domain_ids"] = linked_domain_ids
        if linked_collection_ids is not None:
            body["linked_collection_ids"] = linked_collection_ids
        if preferences is not None:
            body["preferences"] = preferences
        if timeline_days is not None:
            body["timeline_days"] = timeline_days
        if start_date is not None:
            body["start_date"] = start_date
        if category_ids is not None:
            body["category_ids"] = category_ids
        result = self._request("POST", "/api/quests/start", body=body) or {}
        if result.get("pending_approval"):
            decision_id = result.get("decision_id")
            raise QuestPendingApproval(
                f"The quest was NOT created. Quest filed ask {decision_id} and is waiting for a "
                "person to approve it. There is no quest id yet, so nothing can be attached to it. "
                "Tell the person that ask is waiting; do not report a quest as created.",
                decision_id=decision_id)
        return result

    def attach_quest_to_team(self, team_id: str, quest_id: str) -> Dict[str, Any]:
        """POST /api/teams/{team_id}/quest — attach an account-wide quest to a team's board.

        Requires the caller be that team's admin. Raises on failure (same contract as
        ``start_quest``): a caller reporting "attached" must know it landed.
        """
        self._require()
        return self._request("POST", f"/api/teams/{team_id}/quest",
                             body={"quest_id": quest_id}) or {}

    def write_quest_fields(self, quest_id: str, fields: Dict[str, Any], *,
                           team_id: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/teams/{team_id}/write — role-scoped write of QuestState fields.

        The server decides what the CALLER's role may write and returns
        ``{"ok", "blocked", "held"}`` rather than raising on a partial refusal: ``blocked`` names
        fields this role cannot write, ``held`` explains a sanity guard that stopped an autonomous
        AI write. A caller that ignores those two keys will believe every write landed.

        Field scopes at time of writing: an owner/admin may write intent (``outcome``,
        ``acceptance_criteria``, ``quest_goal``, ``quest_completion_criteria``, ``timeline_days``,
        ``start_date``), completion (``completed``, ``goal_achieved``, ``setup_complete``),
        strategy (``strategies``) and progress (``plan``, ``progress``, ``confidence``,
        ``achievements``, ...); a plain member only progress; the AI service member progress and
        strategy, never intent or completion. ``current_state`` is in no role scope HERE; it is
        written by the owner through ``edit_quest_field`` instead, not unwritable.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to write quest fields")
            return self._request("POST", f"/api/teams/{tid}/write",
                                 body={"quest_id": quest_id, "fields": dict(fields)}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("write_quest_fields failed for quest %s: %s", quest_id, e)
            return {}

    def list_quest_workflows(self, quest_id: str) -> Optional[List[Dict[str, Any]]]:
        """GET /api/quests/{quest_id}/workflows: every workflow touching this quest the caller may
        view, each with its steps (actor, window, derived status) and edges.

        ``[]`` when the read worked and the quest has none; ``None`` when the read FAILED, so a
        caller never reports "no workflows" for an outage.
        """
        try:
            self._require()
            resp = self._request("GET", f"/api/quests/{quest_id}/workflows") or {}
            items = resp.get("items") if isinstance(resp, dict) else None
            return [w for w in items if isinstance(w, dict)] if isinstance(items, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_workflows failed for quest %s: %s", quest_id, e)
            self.last_error = str(e)   # a tool reports why, so a repeat of it can be recognised
            return None

    def get_quest(self, quest_id: str, *, team_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/teams/{team_id}/quests/{quest_id} — fetch a single quest by ID.

        Returns quest metadata: quest_id, outcome, completed, owner_user_ids, and other context.
        Requires team_id either here or on the client instance. Returns {} if not found.

        The quest need not be on the CLIENT'S team -- same reason as ``list_quest_goals``: a lane
        can serve several teams, an owner-scoped lane syncs quests across all of its owner's
        teams, and a quest can be moved between teams at any time. So when the caller named no
        team: the quest's known owning team is used first (a cache lookup, never a quest-list
        call, so a single-team lane pays nothing), and if the team that was tried fails, the
        owning team is resolved from the live list and the read retried once there. Before this,
        a quest outside the client's home team returned ``{}`` silently, forever. An explicit
        ``team_id`` is honored as given and never second-guessed.
        """
        try:
            self._require()
            tid = team_id or self._quest_team_cache.get(quest_id) or self.team_id
            if not tid and not team_id:
                tid = self.owning_team_for(quest_id)
            if not tid:
                raise QuestNotConfigured("team_id is required to get a quest")
            try:
                return self._request("GET", f"/api/teams/{tid}/quests/{quest_id}") or {}
            except QuestApiError:
                if team_id:
                    raise  # caller named the team: their call, their error
                # The team that failed may be a stale cache entry (the quest moved) or simply the
                # wrong one (the quest was never ours). Drop it and ask the live list once.
                if self._quest_team_cache.get(quest_id) == tid:
                    self.forget_owning_team(quest_id)
                owner_tid = self.owning_team_for(quest_id)
                if not owner_tid or owner_tid == tid:
                    raise
                log.info("quest %s is not on team %s; using its own team %s",
                         quest_id, tid, owner_tid)
                return self._request(
                    "GET", f"/api/teams/{owner_tid}/quests/{quest_id}") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_quest failed for quest %s: %s", quest_id, e)
            return {}

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

    def add_quest_strategy(self, quest_id: str, title: str, description: str = "", *,
                           actor: str = "ai", user_requested: bool = False) -> Dict[str, Any]:
        """POST /api/quests/{quest_id}/strategies/add -- add one strategy to a quest.

        ``actor``/``user_requested`` mean exactly what they mean on ``edit_quest_field``: a backend
        that gates AI strategy writes answers 202 ``{"applied": False, "reason": ...}`` instead of
        adding it, which is returned as-is so the caller can report it honestly. Returns {} on
        failure rather than raising.
        """
        try:
            return self._request(
                "POST", f"/api/quests/{quest_id}/strategies/add",
                body={"title": title, "description": description or "",
                      "actor": actor, "userRequested": bool(user_requested)}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_quest_strategy failed for quest %s: %s", quest_id, e)
            return {}

    def create_habit(self, quest_id: str, strategy_id: str, name: str, *,
                     frequency: str = "daily", start_date: Optional[str] = None,
                     **fields: Any) -> Dict[str, Any]:
        """POST /api/planning/habits -- create one habit under a quest strategy.

        ``fields`` passes the endpoint's optional keys through in snake_case (``description``,
        ``days``, ``time``, ``duration``, ``linked_goal_id`` ...); ``start_date`` defaults to today.
        Raises ``QuestApiError``/``QuestNotConfigured`` like ``create_goal``: a caller that says
        "habit added" must know it was.
        """
        from datetime import date
        body: Dict[str, Any] = {"quest_id": quest_id, "strategy_id": strategy_id, "name": name,
                                "frequency": frequency,
                                "start_date": start_date or date.today().isoformat()}
        body.update({k: v for k, v in fields.items() if v is not None})
        return self._request("POST", "/api/planning/habits", body=body) or {}

    def list_experiments(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET /api/quests/{quest_id}/research-questions: the quest's experiments.

        The default question (the one the quest's creation inquiry asked) comes first. Raises
        ``QuestApiError``/``QuestNotConfigured``: an empty list must mean "none", never "failed".
        """
        resp = self._request("GET", f"/api/quests/{quest_id}/research-questions") or {}
        rows = resp.get("research_questions") if isinstance(resp, dict) else resp
        return [r for r in (rows or []) if isinstance(r, dict)]

    def create_experiment(self, quest_id: str, question: str, **fields: Any) -> Dict[str, Any]:
        """POST /api/quests/{quest_id}/research-questions: create one experiment.

        ``fields`` are the endpoint's optional keys: ``context``, ``hypotheses`` (``[{statement, sub_question_id?}]``),
        ``purpose``, ``sub_questions`` (``[{question, answer_method?}]``), ``assumptions`` (list of str),
        ``linked_goal_ids``, ``linked_collection_ids``, ``start_date``, ``end_date``,
        ``conditions``. Raises like ``create_habit``.
        """
        body: Dict[str, Any] = {"question": question}
        body.update({k: v for k, v in fields.items() if v is not None})
        return self._request("POST", f"/api/quests/{quest_id}/research-questions", body=body) or {}

    def update_experiment(self, quest_id: str, research_question_id: str,
                          **changes: Any) -> Dict[str, Any]:
        """PATCH /api/quests/{quest_id}/research-questions/{id}: change only the keys given.

        Keys: ``question``, ``context``, ``hypotheses`` (``[{statement, sub_question_id?}]``), ``purpose``, ``sub_questions`` and
        ``assumptions`` (each replaces the whole list), ``status``, ``conclusion``,
        ``linked_goal_ids``, ``linked_collection_ids``, ``start_date``, ``end_date`` (``""`` clears
        a date), ``conditions``. Raises like ``create_habit``.
        """
        return self._request(
            "PATCH", f"/api/quests/{quest_id}/research-questions/{research_question_id}",
            body=changes) or {}

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

    def archive_quest(self, quest_id: str) -> bool:
        """POST /api/quests/{quest_id}/archive — archive one of the caller's own quests.

        Owner-only (the endpoint enforces it server-side): makes the quest inactive without
        deleting it, freeing a slot within the account's quest limit. ``unarchive_quest`` reverses
        it. Returns True only when the archive succeeded.
        """
        try:
            self._require()
            self._request("POST", f"/api/quests/{quest_id}/archive")
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("archive_quest failed for quest %s: %s", quest_id, e)
            return False

    def unarchive_quest(self, quest_id: str) -> bool:
        """POST /api/quests/{quest_id}/unarchive — restore an archived quest to active.

        Owner-only; fails if the account has no room left within its quest limit. Returns True
        only when the unarchive succeeded.
        """
        try:
            self._require()
            self._request("POST", f"/api/quests/{quest_id}/unarchive")
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("unarchive_quest failed for quest %s: %s", quest_id, e)
            return False

    def list_quest_notes(self, quest_id: str, limit: int = DEFAULT_NOTES_LIMIT) -> List[Dict[str, Any]]:
        """GET /api/quests/{quest_id}/notes — the goal's freeform notes, oldest -> newest.

        Returns at most the NEWEST ``limit`` notes (default 200), fetched a page at a time with
        the paging contract (``limit``/``before``, newest first), so a pull never downloads or
        renders an unbounded list: one prod quest once held 34,616 notes. The result is a
        :class:`NotesList` (a plain ``list``) whose ``.total`` is how many notes the quest has in
        all, so a caller can say "N older notes not shown". Against a backend that predates paging
        the bare list it answers is kept as-is (newest ``limit`` of it) and ``.total`` is its length.

        The "goal is the hub" surface: any owner or active-share holder can read them. Returns []
        if not found or inaccessible.
        """
        try:
            self._require()
            collected: List[Dict[str, Any]] = []  # newest first
            total: Optional[int] = None
            cursor: Optional[str] = None
            while len(collected) < limit:
                resp = self._request("GET", f"/api/quests/{quest_id}/notes", params={
                    "limit": min(NOTES_PAGE_SIZE, limit - len(collected)), "before": cursor})
                if isinstance(resp, list):  # a backend without paging answers the whole list
                    legacy = [n for n in resp if isinstance(n, dict)]
                    return NotesList(legacy[-limit:], total=len(legacy))
                if not isinstance(resp, dict):
                    break
                page = [n for n in (resp.get("notes") or []) if isinstance(n, dict)]
                collected.extend(page)
                total = resp.get("unfiltered_total", resp.get("total", total))
                cursor = resp.get("next_cursor")
                if not cursor or not page:
                    break
            collected.reverse()
            return NotesList(collected, total=total if total is not None else len(collected))
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_notes failed for quest %s: %s", quest_id, e)
            return NotesList([], total=0)

    def update_outcome_progress(self, quest_id: str, outcome_id: str, progress_pct: Any,
                                note: str, current_value: Any = None) -> Dict[str, Any]:
        """POST /api/quests/{quest_id}/measurable-outcomes/{outcome_id}/ai-progress

        Record how far one measurable outcome has come, as the AI. The backend applies the evidence
        rules (a generic, thin or repeated note is refused; a rise over 25 points or any drop needs
        a longer note saying what changed) and logs the writer as ``ai:<user id>``. Returns the
        stored outcome. Only progress fields change; the outcome's text and done flag never do.
        When the user's permission for this is "ask first" the backend answers 202 and the return
        value is ``{"pending_approval": True, "decision_id", "message"}``: nothing was written yet.

        ``progress_pct`` and ``current_value`` may arrive as strings (the shell CLI passes
        positionals as text); they are converted here. A refused write RAISES ``QuestApiError``
        whose message is ``Not done: <the backend's reason, verbatim>``, so the caller (a model,
        through ``quest-ai-runner quest update_outcome_progress ... --write``) reads exactly why and
        can rewrite the note with new specifics instead of resending it.
        """
        try:
            body: Dict[str, Any] = {"progress_pct": float(progress_pct), "note": note}
            if current_value is not None:
                body["current_value"] = float(current_value)
        except (TypeError, ValueError):
            raise QuestApiError("Not done: progress_pct and current_value must be numbers "
                                "(progress_pct is 0 to 100)")
        try:
            result = self.post_outcome_progress(quest_id, outcome_id, body)
        except QuestApiError as e:
            if e.status not in (400, 403, 404, 422):
                raise
            if e.status == 404:
                # The AI usually holds an outcome's WORDS, not its id. Resolve them against the
                # quest's own outcomes once, so the call that named the outcome in words works;
                # when nothing (or several things) match, say which outcomes there are.
                resolved, why = self.resolve_outcome_id(quest_id, outcome_id)
                if resolved and resolved != outcome_id:
                    try:
                        result = self.post_outcome_progress(quest_id, resolved, body)
                        return self.progress_result(result)
                    except QuestApiError as retry_error:
                        e = retry_error
                elif why:
                    raise QuestApiError(f"Not done: {why}", status=e.status) from e
            raise QuestApiError(f"Not done: {self.error_detail(e)}", status=e.status) from e
        return self.progress_result(result)

    def post_outcome_progress(self, quest_id: str, outcome_id: str, body: Dict[str, Any]) -> Any:
        return self._request(
            "POST", f"/api/quests/{quest_id}/measurable-outcomes/{outcome_id}/ai-progress",
            body=body)

    def resolve_outcome_id(self, quest_id: str, outcome: str):
        """``(outcome_id, problem)`` for an outcome named by id OR by (part of) its text.

        An id of this quest's outcomes resolves to itself. Otherwise a case-insensitive match on the
        outcome text (the whole text, or a fragment unique to one outcome) or a 1-based position
        resolves to that outcome's id. ``problem`` is a plain sentence listing the quest's outcomes
        with their ids when nothing or several match, so the next call can be right. ``(None, None)``
        when the quest's outcomes could not be read at all.
        """
        state = self.get_my_quest(quest_id)
        quest = (state.get("state") if isinstance(state.get("state"), dict) else state) or {}
        outcomes = [o for o in (quest.get("measurable_outcomes") or quest.get("measurableOutcomes")
                                or []) if isinstance(o, dict) and o.get("id")]
        if not outcomes:
            return None, None
        wanted = str(outcome or "").strip().lower()
        for o in outcomes:
            if str(o["id"]).lower() == wanted:
                return str(o["id"]), None
        if wanted.isdigit() and 1 <= int(wanted) <= len(outcomes):
            return str(outcomes[int(wanted) - 1]["id"]), None
        hits = [o for o in outcomes if wanted and wanted in str(o.get("text") or "").strip().lower()]
        if len(hits) == 1:
            return str(hits[0]["id"]), None
        listing = "; ".join(f"{o['id']} = {str(o.get('text') or '')[:70]!r}" for o in outcomes)
        what = ("matches several of this quest's outcomes" if hits
                else "is not an id or the words of any of this quest's outcomes")
        return None, f"{outcome!r} {what}. Use one of these ids: {listing}"

    @staticmethod
    def progress_result(result: Any) -> Dict[str, Any]:
        if (result or {}).get("pending_approval"):
            # The user's permission for this is "ask first": the write is parked on a decision and has
            # NOT happened. Say so; never hand back an empty outcome that reads like a silent success.
            return {"pending_approval": True, "decision_id": result.get("decision_id"),
                    "message": result.get("message") or "Waiting for a person to approve this change."}
        return (result or {}).get("outcome") or {}

    def add_quest_note(self, quest_id: str, text: str,
                       *, author_label: Optional[str] = None,
                       relayed_author_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """POST /api/quests/{quest_id}/notes — append a note; returns the updated notes list.

        Attribution is derived server-side from the caller: an API-key caller (this client) is
        recorded ``author_kind: "ai"``. Returns [] on failure.

        ``author_label`` names the PERSONA that wrote it ("Bailey"), for a quest several personas
        work. The backend honors it only for API-key callers and falls back to a plain "AI
        assistant", so it can never be used to post as the person: the one name an AI note must not
        carry is the account owner's, since that is what made a run's own summaries indistinguishable
        from the person's replies.

        ``relayed_author_email`` says this note carries a REAL PERSON's own words rather than the
        AI's own summary -- someone who wrote in to a mailbox this account reads, say, and this
        call is passing their message onto the quest it concerns (see
        ``runner.context_updates.InboundMailSource``). ``author_kind`` still records ``"ai"`` (an
        API key did post it), but the backend stores ``author_name`` as ``"<email> (relayed)"`` and,
        critically, enters the note in the account's own asks record under this address -- an
        ordinary API-key note is deliberately never tracked as an ask, and this is the one field
        that says "track this one, and whose it is." Must be a real-looking address
        (``local@domain.tld``); a non-blank value that fails that shape is rejected by the backend
        (HTTP 400) with NOTHING saved, so pass a clean address or leave this None.
        """
        try:
            self._require()
            body: Dict[str, Any] = {"text": text}
            if author_label and str(author_label).strip():
                body["author_label"] = str(author_label).strip()[:60]
            if relayed_author_email and str(relayed_author_email).strip():
                body["relayed_author_email"] = str(relayed_author_email).strip().lower()[:320]
            resp = self._request(
                "POST", f"/api/quests/{quest_id}/notes", body=body) or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_quest_note failed for quest %s: %s", quest_id, e)
            return []

    def list_quest_events(self, quest_id: str, *, since: Optional[str] = None,
                          limit: int = 20, actor: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/quests/{quest_id}/events?since=&limit=&actor= — this quest's lifecycle
        analytics events (quest_created, quest_updated, quest_completed, quest_deleted,
        quest_milestone_completed, quest_duration_updated, quest_archived), newest first.

        Each row is ``{event_type, event_data, timestamp, user_id, actor}``. ``since`` is an
        ISO-8601 timestamp; only events strictly after it come back, the same watermark
        semantics ``runner.context_updates.QuestEventsSource`` needs from every source it reads.
        ``actor`` narrows to one value -- the source always passes ``"app"``, because an API-key
        write (an AI task, autopilot, this very runner) authenticates AS the quest owner and
        would otherwise be indistinguishable from one the person made themselves; see that
        class's docstring for why that filter, not the endpoint, is where the honesty lives.

        Returns [] on any failure: unconfigured client, network, or a backend old enough not to
        have this route yet. That last case is not hypothetical -- this endpoint and the
        ``actor`` field it depends on both landed the same day this method did, so a lane still
        pointed at an older deployment must degrade to "no events" rather than raise.
        """
        try:
            self._require()
            params: Dict[str, Any] = {"limit": limit}
            if since:
                params["since"] = since
            if actor:
                params["actor"] = actor
            resp = self._request("GET", f"/api/quests/{quest_id}/events", params=params) or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_events failed for quest %s: %s", quest_id, e)
            return []

    def list_habits(self) -> List[Dict[str, Any]]:
        """GET /api/planning/habits/all — every habit on the caller's account. Returns [] on error."""
        try:
            self._require()
            resp = self._request("GET", "/api/planning/habits/all")
            if isinstance(resp, list):
                return resp
            items = (resp.get("habits") or resp.get("items")) if isinstance(resp, dict) else None
            return items if isinstance(items, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_habits failed: %s", e)
            return []

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

        The backend request model (``UpdateAutopilotRequest``, ``extra="forbid"``) accepts ``mode``
        (one of ``off`` / ``suggest`` / ``act`` / ``reactive``), ``planning``, ``cadence``,
        ``run_time``, ``run_timezone``, ``personas``, ``env_id``, ``model``, ``adopt_recurring``,
        ``instructions``, ``last_pass_at``, ``miss_streak``, ``helpful``, ``unhelpful``, and a
        nested ``email`` object (``UpdateAutopilotEmailRequest``): ``enabled`` (bool),
        ``recipients`` (list of real addresses, max 5 -- an empty list falls back to the owner's
        own account address), ``reply_label`` (auto-derived from the quest's outcome text when
        omitted), ``regenerate_reply_token`` (bool). There is no client-settable ``reply_token``
        field -- toggling ``email.enabled`` from off to on mints one server-side and builds the
        quest's ``reply_address`` from it, returned in THIS call's own response body under
        ``autopilot.email.reply_address``; it cannot be predicted in advance, only read back from
        here. ``extra="forbid"`` means an unrecognized key now returns a 422, not a silent no-op --
        the caller must still VERIFY the echoed settings for anything it cares landed (see
        ``runner.autopilot._update_pass_bookkeeping``, which reads them back and reports loudly
        when a field it wrote did not stick, rather than letting the cadence gate silently never
        advance).

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
