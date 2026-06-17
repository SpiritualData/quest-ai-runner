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
        rep_id: Optional[str] = None,
        task_type: Optional[str] = None,
    ):
        """Initialize with Quest client and optional filters.

        Args:
            quest_client: A QuestClient instance (configured with base_url, api_key, team_id).
            team_id: Override team_id (uses client's if not provided).
            rep_id: Filter guidance to this rep (rep:rep_id tags).
            task_type: Filter guidance to this task type (task:task_type tags).
        """
        self.client = quest_client
        self.team_id = team_id or (quest_client.team_id if quest_client else None)
        self.rep_id = rep_id
        self.task_type = task_type
        self._last_cards: List[Dict[str, Any]] = []

    def __call__(self) -> List[Dict[str, Any]]:
        """Fetch dynamic guidance from Quest backend.

        Returns: List of guidance card dicts {id, title, body, tags, description, ...}.
        """
        if not self.client or not self.client.configured:
            log.debug("Quest client not configured; no dynamic guidance available")
            return []

        try:
            cards = self.client.list_guidance_cards(
                rep_id=self.rep_id,
                task_type=self.task_type,
                team_id=self.team_id,
                limit=100,
            )
            self._last_cards = cards or []
            if cards:
                log.debug(f"Loaded {len(cards)} guidance cards from Quest")
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
