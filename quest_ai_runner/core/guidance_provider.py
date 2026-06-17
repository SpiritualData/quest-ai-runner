"""Universal guidance provider — unified interface for guidance cards from any environment.

Implements the GuidanceProvider protocol for the orchestrator, backed by
GuidanceCardManager for auto-detecting and syncing guidance changes across
all environments (Quest backend, external runners, custom deployments).

Guidance cards are loaded once per process and auto-reloaded on changes,
making them immediately available without restarts or manual syncing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import GuidanceCard as CoreGuidanceCard
from quest_ai_runner.core.adapters import GuidanceProviderBase


class UniversalGuidanceProvider(GuidanceProviderBase):
    """Guidance provider backed by GuidanceCardManager.

    Loads guidance cards from a configurable directory, auto-detects changes,
    and provides them to the orchestrator for pre-flight context injection.

    Each environment (Quest backend, local runner, etc.) can specify its own
    guidance cards directory; cards are loaded once and auto-reloaded on changes.
    """

    def __init__(self, cards_dir: Optional[str] = None):
        """Initialize with a guidance cards directory.

        Args:
            cards_dir: Path to guidance cards directory. If None, auto-resolves
                      standard locations (.quest-guidance, /app/prompts/guidance, etc.).
        """
        from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager

        self.manager = GuidanceCardManager(cards_dir=cards_dir)
        self._cards_cache: List[CoreGuidanceCard] = []
        self._cache_valid = False

    def select(self, user_message: str, *, limit: int = 5) -> List[CoreGuidanceCard]:
        """Select the most relevant guidance cards for a user message.

        Auto-reloads cards if changes detected. Uses simple keyword matching
        (title + description) to find relevant cards. More sophisticated semantic
        selection can be added via vector stores.

        Args:
            user_message: The user's input message.
            limit: Max cards to return.

        Returns:
            List of relevant GuidanceCard objects.
        """
        # Reload if changes detected
        if self.manager.has_changes() or not self._cache_valid:
            cards = self.manager.load_cards()
            self._cards_cache = [self._to_core_card(c) for c in cards]
            self._cache_valid = True

        if not self._cards_cache:
            return []

        # Simple keyword matching: score cards by term overlap
        msg_words = set(user_message.lower().split())
        scored = []
        for card in self._cards_cache:
            # Score based on matches in title + description
            title_words = set(card.title.lower().split())
            desc_words = set(card.description.lower().split()) if card.description else set()
            searchable = title_words | desc_words

            matches = len(msg_words & searchable)
            if matches > 0 or not msg_words:
                # Return all cards if no query terms; otherwise score by matches
                score = matches if msg_words else 1
                scored.append((score, card))

        # Sort by score (descending) and return top-k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [card for _, card in scored[:limit]]

    def list_cards(self) -> List[CoreGuidanceCard]:
        """List all available guidance cards."""
        if self.manager.has_changes() or not self._cache_valid:
            cards = self.manager.load_cards()
            self._cards_cache = [self._to_core_card(c) for c in cards]
            self._cache_valid = True
        return self._cards_cache

    def read_card(self, card_id: str) -> Optional[CoreGuidanceCard]:
        """Read a single card by ID."""
        # Reload if needed
        if self.manager.has_changes() or not self._cache_valid:
            self.list_cards()  # Triggers reload

        for card in self._cards_cache:
            if card.id == card_id:
                return card
        return None

    def save_card(self, card_id: str, title: str, body: str, **metadata) -> bool:
        """Save/update a guidance card.

        Args:
            card_id: Card identifier.
            title: Card title.
            body: Card markdown body.
            **metadata: Additional frontmatter (description, tags, etc.).

        Returns:
            True if saved, False on error.
        """
        if self.manager.save_card(card_id, title, body, **metadata):
            self._cache_valid = False  # Invalidate cache
            return True
        return False

    def delete_card(self, card_id: str) -> bool:
        """Delete a guidance card."""
        if self.manager.delete_card(card_id):
            self._cache_valid = False  # Invalidate cache
            return True
        return False

    @staticmethod
    def _to_core_card(card: Any) -> CoreGuidanceCard:
        """Convert GuidanceCardManager card to core GuidanceCard."""
        return CoreGuidanceCard(
            id=card.id,
            title=card.title,
            body=card.body,
            description=card.description,
            tags=card.tags or [],
        )
