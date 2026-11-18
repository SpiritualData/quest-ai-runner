"""Scheduling links (the owner's booking pages): read-only list and bookings.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
Read-only: nothing here creates, changes or cancels a link or a booking.
"""
from __future__ import annotations

from typing import Any, Dict, List

from quest_ai_runner.body.internal_context.quest.client.quest_client_support import QuestApiError, QuestNotConfigured


class SchedulingMixin:
    """Read the owner's scheduling links and the bookings made on them."""

    def list_scheduling_links(self) -> List[Dict[str, Any]]:
        """GET /api/scheduling/links: the signed-in owner's scheduling links (id, slug, title, is_active).

        Read-only. Raises ``QuestApiError`` or ``QuestNotConfigured`` on failure, so a caller can
        tell "no links" (an empty list) from "could not read" (an exception).
        """
        self._require()
        resp = self._request("GET", "/api/scheduling/links")
        return resp if isinstance(resp, list) else []

    def list_link_bookings(self, link_id: str) -> List[Dict[str, Any]]:
        """GET /api/scheduling/links/{link_id}/bookings: every booking made on one of the owner's links.

        Read-only. Each booking is the backend's camelCase object: ``id``, ``linkId``, ``visitorName``,
        ``visitorEmail``, ``fieldValues``, ``start``, ``end``, ``status`` (``confirmed`` or ``cancelled``),
        ``createdAt``. Oldest start first. A link the owner does not have is a 404, which raises
        ``QuestApiError``; so does any other failure. An empty list means the link has no bookings.
        """
        self._require()
        if not link_id:
            raise QuestNotConfigured("link_id is required to list a scheduling link's bookings")
        resp = self._request("GET", f"/api/scheduling/links/{link_id}/bookings")
        if not isinstance(resp, list):
            raise QuestApiError(f"Quest API GET /api/scheduling/links/{link_id}/bookings returned no list")
        return resp
