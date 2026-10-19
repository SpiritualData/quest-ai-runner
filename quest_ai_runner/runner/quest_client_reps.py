"""AI reps and guidance: rosters, profile, corrections and guidance cards.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .quest_client_support import QuestApiError, QuestNotConfigured, log


class RepsMixin:
    """AI reps and guidance: rosters, profile, corrections and guidance cards."""

    # --- team AI-rep registry (list/create; see get_ai_profile/update_ai_profile for ONE rep) -----

    def list_team_reps(self, *, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/reps — every AI rep registered on a team.

        A "rep" is a first-class persona in the team's AI-rep registry (see ``create_rep``), keyed
        by ``rep_id`` and (usually) a ``display_name``. Distinct from ``list_open_decisions_for_user``
        and from a plain human team member. Returns [] on any error.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to list team reps")
            resp = self._request("GET", f"/api/teams/{tid}/reps") or {}
            reps = resp.get("reps") if isinstance(resp, dict) else None
            return reps if isinstance(reps, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_team_reps failed: %s", e)
            return []

    def list_team_chat(self, *, team_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """GET /api/teams/{team_id}/chat — the team chat's recent messages, oldest first.

        Read-only. ``limit`` caps how many of the latest messages come back. Returns [] on any error.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not tid:
                raise QuestNotConfigured("team_id is required to read the team chat")
            resp = self._request("GET", f"/api/teams/{tid}/chat", params={"limit": limit}) or {}
            msgs = resp.get("messages") if isinstance(resp, dict) else None
            return msgs if isinstance(msgs, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_team_chat failed: %s", e)
            return []

    def create_rep(self, display_name: str, *, team_id: Optional[str] = None,
                   persona: Optional[str] = None, area: Optional[str] = None,
                   owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/reps — register a new AI rep on a team.

        Omit ``owner_user_id`` for a PERSON-LESS rep (a character/persona that represents no real
        team member, just a named AI voice); pass it to attach the rep to a real person instead.
        ``persona`` is the rep's seed instructions/voice; ``area`` is a short human label for what
        it's for. Raises ``QuestApiError``/``QuestNotConfigured`` on failure rather than swallowing
        it: a caller that stores the returned ``rep_id`` must know it actually exists.
        """
        self._require()
        tid = team_id or self.team_id
        if not tid:
            raise QuestNotConfigured("team_id is required to create a rep")
        body: Dict[str, Any] = {"display_name": display_name, "teams": [tid]}
        if persona is not None:
            body["persona"] = persona
        if area is not None:
            body["area"] = area
        if owner_user_id is not None:
            body["owner_user_id"] = owner_user_id
        return self._request("POST", "/api/reps", body=body) or {}

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
