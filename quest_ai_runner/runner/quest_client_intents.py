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

from .quest_client_support import QuestApiError, QuestNotConfigured, log


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
            "lockedFields": list(detail.get("lockedFields") or [])}


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
                     force: bool = False) -> Dict[str, Any]:
        """POST .../refresh: the one hash-gated spec update. Unchanged inputs write nothing (``skipped``)."""
        return self.intent_write("POST", f"/api/intents/specs/{subject_type}/{subject_id}/refresh",
                                  {"reason": reason, "force": bool(force)})

    def edit_spec_record(self, record_id: str, fields: Dict[str, Any], expected_revision: int,
                         *, status: Optional[str] = None) -> Dict[str, Any]:
        """PATCH /api/intents/records/{recordId}. A field the person edited answers 409 FIELD_LOCKED."""
        body: Dict[str, Any] = {"expectedRevision": int(expected_revision)}
        if fields:
            body["fields"] = fields
        if status:
            body["status"] = status
        return self.intent_write("PATCH", f"/api/intents/records/{record_id}", body)

    def place_quest(self, quest_id: str, intent_id: Optional[str], *, implementation_id: Optional[str] = None,
                    owner_type: str = "user", owner_id: Optional[str] = None) -> Dict[str, Any]:
        """PUT /api/intents/placements for one quest (``intent_id`` None puts it back to unsorted)."""
        move: Dict[str, Any] = {"questId": quest_id, "intentId": intent_id}
        if implementation_id:
            move["implementationId"] = implementation_id
        body: Dict[str, Any] = {"ownerType": owner_type, "moves": [move]}
        if owner_id:
            body["ownerId"] = owner_id
        return self.intent_write("PUT", "/api/intents/placements", body)

    def intent_write(self, method: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._request(method, path, body=body) or {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("%s %s refused: %s", method, path, e)
            self.last_error = str(e)
            return refusal_of(e)
