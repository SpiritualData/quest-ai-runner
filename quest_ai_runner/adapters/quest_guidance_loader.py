"""Quest dynamic guidance loader — fetch guidance from Quest backend database.

For Quest backend deployments, this loader pulls guidance cards from the Quest
database (feedback cards, rep corrections, environment guidance) so they're
available to UniversalGuidanceProvider.select().

Guidance is stored in Quest's guidance collection and includes:
- Feedback-derived guidance cards
- Rep-specific corrections (tagged rep:user_id)
- Task-type guidance (tagged task:*)
- Environment-wide principles

This enables:
- Guidance synced across all instances
- Database-backed persistence
- Easy editing/management via Quest UI
- Dynamic loading without file system dependencies
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.guidance-loader")


class QuestGuidanceLoader:
    """Load guidance cards from Quest backend database.

    Implements the dynamic_guidance_loader interface for UniversalGuidanceProvider.
    Fetches guidance cards from Quest via QuestClient.list_guidance_cards().
    """

    def __init__(
        self,
        quest_client: Any,  # QuestClient
        team_id: Optional[str] = None,
        org_id: Optional[str] = None,
        rep_id: Optional[str] = None,
    ):
        """Initialize with Quest client and scope context.

        Args:
            quest_client: A QuestClient instance (configured with base_url, api_key, team_id).
            team_id: Team context (defaults to client's team_id).
            org_id: Organization context (optional, for org-level guidance).
            rep_id: AI rep context (optional, for rep-specific guidance filtering).

        Loads guidance at all applicable scopes: global, org, team, rep.
        """
        self.client = quest_client
        self.team_id = team_id or (quest_client.team_id if quest_client else None)
        self.org_id = org_id
        self.rep_id = rep_id
        self._last_cards: List[Dict[str, Any]] = []

    def __call__(self) -> List[Dict[str, Any]]:
        """Fetch dynamic guidance from Quest backend at all applicable scopes.

        Fetches guidance cards matching all scopes: global, org, team, rep.
        Scopes are not exclusive — a card can be tagged with multiple scopes.

        Returns: List of guidance card dicts {id, title, body, tags, description, ...}.
        """
        if not self.client or not self.client.configured:
            log.debug("Quest client not configured; no dynamic guidance available")
            return []

        try:
            # Fetch guidance at all applicable scopes
            # The API should return cards matching any of these scopes
            cards = self.client.list_guidance_cards(
                team_id=self.team_id,
                rep_id=self.rep_id,
                limit=200,  # Higher limit to get all scopes
            )
            self._last_cards = cards or []
            if cards:
                log.debug(
                    f"Loaded {len(cards)} guidance cards from Quest "
                    f"(team={self.team_id}, rep={self.rep_id})"
                )
            return self._last_cards
        except Exception as e:  # noqa: BLE001
            log.error(f"Failed to load guidance from Quest: {e}")
            return []

    def reload(self) -> List[Dict[str, Any]]:
        """Explicitly reload guidance cards from Quest."""
        return self()

    def get_last_loaded(self) -> List[Dict[str, Any]]:
        """Get the last successfully loaded guidance cards (cached)."""
        return self._last_cards
