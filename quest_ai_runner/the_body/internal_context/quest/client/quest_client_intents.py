"""Life intents and the hidden specification: the Quest client calls the AI tools use.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
Each method calls one documented endpoint under ``/api/intents`` and sends the camelCase wire shape.
The caller is identified by its API key alone: the actor is never part of a request body, so the
server treats every call from here as the AI's and applies the user-edit lock and the ask rules.

Return shapes, so a tool never prints a refusal as success:

* a read returns the response, or ``None`` when it failed (``last_error`` says why), so "nothing
  there" and "could not read" stay different;
* a write returns the response body on success, which for a held change is
  ``{"pending": True, "decisionId": ...}`` (HTTP 202: a person must say yes first), and
  ``{"refused": True, "status": ..., "code": ..., "message": ..., "lockedFields": [...]}`` when the
  server declined (for example ``FIELD_LOCKED`` because the person edited that field) or the call
  could not be made. A refusal is never an empty dict.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError, QuestNotConfigured, log


def refusal_of(error: Exception) -> Dict[str, Any]:
    """Turn an API error into the explicit refusal a write returns (server code and locked fields kept)."""
    status = getattr(error, "status", None)
    text = str(error)
    detail: Dict[str, Any] = {}
    start = text.find("{")
    if start >= 0:
        try:
            parsed = json.loads(text[start:])
            body = parsed.get("detail", parsed) if isinstance(parsed, dict) else {}
            detail = body if isinstance(body, dict) else {"message": str(body)}
        except ValueError:
            detail = {}
    return {"refused": True, "status": status, "code": detail.get("code") or "",
            "message": detail.get("message") or text,
            "lockedFields": list(detail.get("lockedFields") or []),
            # The request was sent and never answered: the change may have been made (not a refusal at all).
            "outcomeUnknown": bool(getattr(error, "outcome_unknown", False))}


class IntentsMixin:
    """Top intents, quest placements and ESOI specifications."""

    def list_intents(self, *, owner_type: str = "user", owner_id: Optional[str] = None,
                     include_archived: bool = False) -> Optional[Dict[str, Any]]:
        """GET /api/intents/overview: the scope's top intents with their quest ids, and the unsorted quests."""
        try:
            return self._request("GET", "/api/intents/overview", params={
                "ownerType": owner_type, "ownerId": owner_id,
                "includeArchived": "true" if include_archived else None}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_intents failed: %s", e)
            self.last_error = str(e)
            return None

    def get_intent_frame(self, quest_id: str) -> Optional[Dict[str, Any]]:
        """GET /api/intents/frame: why a quest exists. ``{"text", "anchor", "below"}``; ``text`` is empty for an
        unsorted quest or a person with no intents. Computed by the server from stored ids, no model call."""
        try:
            return self._request("GET", "/api/intents/frame", params={"questId": quest_id}) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_intent_frame failed for %s: %s", quest_id, e)
            self.last_error = str(e)
            return None

    def get_spec(self, subject_type: str, subject_id: str) -> Optional[Dict[str, Any]]:
        """GET /api/intents/specs/{subjectType}/{subjectId}: the records of an intent's or a quest's specification."""
        try:
            return self._request("GET", f"/api/intents/specs/{subject_type}/{subject_id}") or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_spec failed for %s %s: %s", subject_type, subject_id, e)
            self.last_error = str(e)
            return None

    def refresh_spec(self, subject_type: str, subject_id: str, *, reason: str = "manual",
                     force: bool = False, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """POST .../refresh: the one hash-gated spec update. Unchanged inputs write nothing (``skipped``)."""
        return self.intent_write("POST", f"/api/intents/specs/{subject_type}/{subject_id}/refresh",
                                  {"reason": reason, "force": bool(force)}, human_statement=human_statement)

    def refresh_metric_values(self, quest_id: str) -> Dict[str, Any]:
        """Refresh the measured values of a quest's metric records that name a source: each source is read through the lane's ``metric_sources`` and the
        reading is written as the record's leading ``Current value:`` line with ``measured_through`` set (nothing else in the record changes; the same
        reading writes nothing). Returns ``{"quest_id", "records"}``, one result per record: written, unchanged, gap, stale, held or refused."""
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.metric_refresh import refresh_metric_values
        return {"quest_id": quest_id, "records": refresh_metric_values(self, quest_id)}

    def create_spec_record(self, subject_type: str, subject_id: str, record_type: str, fields: Dict[str, Any],
                           *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/intents/specs/{subjectType}/{subjectId}/records: one new record (problem, ideal, solution, implementation or metric) of
        the specification. The server answers 403 when the quest's autopilot is off, and a record the AI writes stays the AI's."""
        return self.intent_write("POST", f"/api/intents/specs/{subject_type}/{subject_id}/records", {"type": record_type, "fields": fields},
                                  human_statement=human_statement)

    def edit_spec_record(self, record_id: str, fields: Dict[str, Any], expected_revision: int,
                         *, status: Optional[str] = None, milestone_entries: Optional[List[Dict[str, Any]]] = None,
                         human_statement: Optional[str] = None) -> Dict[str, Any]:
        """PATCH /api/intents/records/{recordId}. A field the person edited answers 409 FIELD_LOCKED unless ``human_statement`` (the
        person's verbatim words asking for this change) is given. ``milestone_entries`` (a solution only)
        replaces the structured milestones with the whole list given."""
        body: Dict[str, Any] = {"expectedRevision": int(expected_revision)}
        if fields:
            body["fields"] = fields
        if status:
            body["status"] = status
        if milestone_entries is not None:
            body["milestoneEntries"] = milestone_entries
        return self.intent_write("PATCH", f"/api/intents/records/{record_id}", body, human_statement=human_statement)

    def archive_spec_record(self, record_id: str, *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """DELETE /api/intents/records/{recordId}: archive one record (refused for the AI unless ``human_statement`` carries the person's words)."""
        return self.intent_write("DELETE", f"/api/intents/records/{record_id}", {}, human_statement=human_statement)

    def create_intent(self, name: str, vision_statement: str = "", *, owner_type: str = "user", owner_id: Optional[str] = None,
                      after_intent_id: Optional[str] = None, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/intents: a new top intent."""
        body: Dict[str, Any] = {"ownerType": owner_type, "name": name, "visionStatement": vision_statement}
        if owner_id:
            body["ownerId"] = owner_id
        if after_intent_id:
            body["afterIntentId"] = after_intent_id
        return self.intent_write("POST", "/api/intents", body, human_statement=human_statement)

    def update_intent(self, intent_id: str, expected_revision: int, *, name: Optional[str] = None,
                      vision_statement: Optional[str] = None, opportunity: Optional[str] = None,
                      human_statement: Optional[str] = None) -> Dict[str, Any]:
        """PATCH /api/intents/{intentId}: rename, change the vision or the opportunity (a person's own held as an ask for the AI)."""
        body: Dict[str, Any] = {"expectedRevision": int(expected_revision)}
        for key, value in (("name", name), ("visionStatement", vision_statement), ("opportunity", opportunity)):
            if value is not None:
                body[key] = value
        return self.intent_write("PATCH", f"/api/intents/{intent_id}", body, human_statement=human_statement)

    def delete_intent(self, intent_id: str, *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """DELETE /api/intents/{intentId}."""
        return self.intent_write("DELETE", f"/api/intents/{intent_id}", {}, human_statement=human_statement)

    def reorder_intents(self, intent_ids: List[str], *, owner_type: str = "user", owner_id: Optional[str] = None,
                        human_statement: Optional[str] = None) -> Dict[str, Any]:
        """PUT /api/intents/order: exactly the owner's active intents in the new order."""
        body: Dict[str, Any] = {"ownerType": owner_type, "intentIds": list(intent_ids)}
        if owner_id:
            body["ownerId"] = owner_id
        return self.intent_write("PUT", "/api/intents/order", body, human_statement=human_statement)

    def suggest_milestones(self, record_id: str, feedback: str = "", *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/intents/records/{recordId}/suggest-milestones: a drafted ladder, not saved."""
        return self.intent_write("POST", f"/api/intents/records/{record_id}/suggest-milestones", {"feedback": feedback},
                                 human_statement=human_statement)

    def set_milestone_met(self, record_id: str, milestone_id: str, met: bool, *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """PATCH /api/intents/records/{recordId}/milestones/{milestoneId}/met: mark a structured milestone met or not met."""
        return self.intent_write("PATCH", f"/api/intents/records/{record_id}/milestones/{milestone_id}/met", {"met": bool(met)},
                                 human_statement=human_statement)

    def place_quest(self, quest_id: str, intent_id: Optional[str], *, implementation_id: Optional[str] = None,
                    owner_type: str = "user", owner_id: Optional[str] = None,
                    human_statement: Optional[str] = None) -> Dict[str, Any]:
        """PUT /api/intents/placements for one quest (``intent_id`` None puts it back to unsorted)."""
        move: Dict[str, Any] = {"questId": quest_id, "intentId": intent_id}
        if implementation_id:
            move["implementationId"] = implementation_id
        body: Dict[str, Any] = {"ownerType": owner_type, "moves": [move]}
        if owner_id:
            body["ownerId"] = owner_id
        return self.intent_write("PUT", "/api/intents/placements", body, human_statement=human_statement)

    def create_sub_intent(self, record_id: str, name: str, opportunity: str, *, vision_statement: str = "",
                          reveal: Optional[str] = None, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/intents/records/{recordId}/sub-intents: make an implementation record of an intent its
        own intent. From an AI caller the server never creates it: it answers 202 ``{"pending": True,
        "decisionId": ...}`` (an ask the person approves in "Asks for you"), which is returned unchanged."""
        body: Dict[str, Any] = {"name": name, "visionStatement": vision_statement, "opportunity": opportunity}
        if reveal:
            body["reveal"] = reveal
        return self.intent_write("POST", f"/api/intents/records/{record_id}/sub-intents", body, human_statement=human_statement)

    def get_publisher_profile(self, user_id: str) -> Dict[str, Any]:
        """GET /api/spec-library/publishers/{userId}: the page a spec's "By <name>" link opens (name, bio, websiteUrl, avatarUrl,
        expertiseTags, canEdit). A refusal dict (``refused``, ``status``) when the server does not have the route yet or the user is unknown."""
        return self.intent_write("GET", f"/api/spec-library/publishers/{user_id}", {})

    def set_publisher_profile(self, user_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /api/spec-library/publishers/{userId}: only the given fields change (name, bio, websiteUrl, avatarUrl, expertiseTags; an empty
        string clears one). Only the user themself may write their own profile; a personal API key acts as its owner here."""
        return self.intent_write("PUT", f"/api/spec-library/publishers/{user_id}", dict(fields))

    def intent_write(self, method: str, path: str, body: Dict[str, Any], *, human_statement: Optional[str] = None) -> Dict[str, Any]:
        """Send one intents write. A non-blank ``human_statement`` (the person's verbatim words asking for exactly this change) is sent as
        ``humanStatement``: the server then treats the call as the person's for its AI guards. Blank or absent sends nothing."""
        if (human_statement or "").strip():
            if method == "DELETE":      # the backend reads a DELETE's words from the query string: a DELETE has no body
                path = f"{path}?{urlencode({'humanStatement': human_statement})}"
            else:
                body = {**body, "humanStatement": human_statement}
        body = body or None     # a bare DELETE sends no body
        try:
            return self._request(method, path, body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("%s %s refused: %s", method, path, e)
            self.last_error = str(e)
            return refusal_of(e)
