"""Universal guidance provider — intelligent guidance from files + dynamic feedback.

Implements GuidanceProvider for the orchestrator, supporting:
- File-based guidance cards (static, versioned, environment-specific)
- Dynamic guidance from human feedback (learned corrections, rep profiles)
- Intelligent selection based on task context, AI rep, and user message
- Semantic search via vector stores (when configured)
- Auto-sync and change detection for both sources

Guidance is injected into the system prompt, making it core behavior that
the AI must follow, not optional context.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from quest_ai_runner.core.adapters import GuidanceCard as CoreGuidanceCard
from quest_ai_runner.core.adapters import GuidanceProviderBase

log = logging.getLogger("quest-ai-runner.guidance")


class UniversalGuidanceProvider(GuidanceProviderBase):
    """Guidance provider supporting both file-based and dynamic sources.

    Sources:
    1. File-based: Markdown cards in configured directory (auto-synced on changes)
    2. Dynamic: Learned guidance from human feedback, rep corrections, etc.

    Selection is context-aware:
    - Task type (plan, answer, deep, confirm)
    - AI rep persona/profile
    - User message content
    - Applicable tags/categories

    Caches cards and auto-reloads on file changes. Supports semantic search
    when a vector store is configured.
    """

    def __init__(
        self,
        cards_dir: Optional[str] = None,
        dynamic_guidance_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        vector_store: Optional[Any] = None,
    ):
        """Initialize with file-based and optional dynamic guidance sources.

        Args:
            cards_dir: Path to guidance cards directory. If None, auto-resolves.
            dynamic_guidance_loader: Optional callable that returns dynamic guidance
                                    dicts (e.g., from rep profiles, feedback DB).
                                    Called each select() to pick up new feedback.
            vector_store: Optional vector store (e.g., Qdrant) for semantic search.
                         When set, guidance cards are indexed for similarity matching.
        """
        from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager

        self.manager = GuidanceCardManager(cards_dir=cards_dir)
        self.dynamic_loader = dynamic_guidance_loader
        self.vector_store = vector_store
        self._cards_cache: List[CoreGuidanceCard] = []
        self._dynamic_cache: List[Dict[str, Any]] = []
        self._cache_valid = False

    def select(
        self,
        user_message: str = "",
        *,
        task_type: Optional[str] = None,
        rep_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[CoreGuidanceCard]:
        """Intelligently select applicable guidance for this task.

        Selection factors (in order of importance):
        1. Exact tag matches (task_type, rep_id)
        2. Semantic similarity to user message (if vector store available)
        3. Keyword matches in title/description
        4. Rep-specific overrides of generic guidance

        Args:
            user_message: The user's input (for semantic/keyword matching).
            task_type: Type of task (plan, answer, deep, confirm, etc.).
            rep_id: The AI rep running this task (affects applicable guidance).
            tags: Additional tags to match (custom categories).
            limit: Max cards to return.

        Returns:
            List of applicable GuidanceCard objects, ordered by relevance.
        """
        # Reload if changes detected
        self._reload_if_needed()

        all_cards = self._cards_cache + [self._to_core_card(d) for d in self._dynamic_cache]
        if not all_cards:
            return []

        scored = []

        for card in all_cards:
            score = 0
            reasons = []

            # Factor 1: Exact tag matches (highest priority)
            card_tags = set(card.tags or [])
            matching_tags = set()

            if task_type and f"task:{task_type}" in card_tags:
                score += 100
                matching_tags.add(f"task:{task_type}")
                reasons.append(f"task_type:{task_type}")

            if rep_id and f"rep:{rep_id}" in card_tags:
                score += 90
                matching_tags.add(f"rep:{rep_id}")
                reasons.append(f"rep:{rep_id}")

            if tags:
                for tag in tags:
                    if tag in card_tags:
                        score += 50
                        matching_tags.add(tag)
                        reasons.append(f"tag:{tag}")

            # Factor 2: Semantic similarity (if vector store available)
            if self.vector_store and user_message:
                try:
                    similarity = self._semantic_score(card, user_message)
                    score += int(similarity * 80)
                    if similarity > 0.5:
                        reasons.append(f"semantic:{similarity:.2f}")
                except Exception:  # noqa: BLE001
                    pass

            # Factor 3: Keyword matching
            if user_message:
                keyword_score = self._keyword_score(card, user_message)
                score += keyword_score
                if keyword_score > 0:
                    reasons.append(f"keyword:{keyword_score}")

            # Include card if it has any relevance or if no filters specified
            if score > 0 or (not task_type and not rep_id and not tags and not user_message):
                scored.append((score, reasons, card))

        # Sort by score (descending) and return top-k
        scored.sort(key=lambda x: x[0], reverse=True)
        result = [card for _, _, card in scored[:limit]]

        # Log selections for debugging
        if result:
            log.debug(
                f"Selected {len(result)} guidance cards (task={task_type}, rep={rep_id}): "
                f"{[c.id for c in result]}"
            )

        return result

    def list_cards(self) -> List[CoreGuidanceCard]:
        """List all available guidance cards (file-based + dynamic)."""
        self._reload_if_needed()
        dynamic_cards = [self._to_core_card(d) for d in self._dynamic_cache]
        return self._cards_cache + dynamic_cards

    def read_card(self, card_id: str) -> Optional[CoreGuidanceCard]:
        """Read a single card by ID."""
        self._reload_if_needed()
        for card in self._cards_cache:
            if card.id == card_id:
                return card
        for card in [self._to_core_card(d) for d in self._dynamic_cache]:
            if card.id == card_id:
                return card
        return None

    def save_card(self, card_id: str, title: str, body: str, **metadata) -> bool:
        """Save/update a guidance card to file."""
        if self.manager.save_card(card_id, title, body, **metadata):
            self._cache_valid = False
            return True
        return False

    def delete_card(self, card_id: str) -> bool:
        """Delete a guidance card from file."""
        if self.manager.delete_card(card_id):
            self._cache_valid = False
            return True
        return False

    def _reload_if_needed(self) -> None:
        """Reload cards if file or dynamic changes detected."""
        if self.manager.has_changes() or not self._cache_valid:
            cards = self.manager.load_cards()
            self._cards_cache = [self._to_core_card(c) for c in cards]

        if self.dynamic_loader and not self._cache_valid:
            try:
                dynamic = self.dynamic_loader()
                self._dynamic_cache = dynamic or []
            except Exception:  # noqa: BLE001
                self._dynamic_cache = []

        self._cache_valid = True

    def _semantic_score(self, card: CoreGuidanceCard, query: str) -> float:
        """Score card by semantic similarity to query (0-1)."""
        if not self.vector_store or not hasattr(self.vector_store, "search"):
            return 0.0

        try:
            searchable = f"{card.title} {card.description} {card.body}"
            results = self.vector_store.search(query, collection_name="guidance", limit=1)
            for result in results or []:
                if result.get("id") == card.id or result.get("payload", {}).get("id") == card.id:
                    score = result.get("score", 0.0)
                    return min(1.0, max(0.0, score))  # Clamp to 0-1
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    def _keyword_score(self, card: CoreGuidanceCard, query: str) -> int:
        """Score card by keyword overlap with query."""
        query_words = set(query.lower().split())
        if not query_words:
            return 0

        searchable = f"{card.title} {card.description}".lower()
        card_words = set(searchable.split())
        matches = len(query_words & card_words)
        return matches * 10  # Each match worth 10 points

    @staticmethod
    def _to_core_card(card_dict: Any) -> CoreGuidanceCard:
        """Convert card dict or object to core GuidanceCard."""
        if isinstance(card_dict, dict):
            return CoreGuidanceCard(
                id=card_dict.get("id", "unknown"),
                title=card_dict.get("title", ""),
                body=card_dict.get("body", ""),
                description=card_dict.get("description", ""),
                tags=card_dict.get("tags", []) or [],
            )
        # Assume it's an object with attributes
        return CoreGuidanceCard(
            id=getattr(card_dict, "id", "unknown"),
            title=getattr(card_dict, "title", ""),
            body=getattr(card_dict, "body", ""),
            description=getattr(card_dict, "description", ""),
            tags=getattr(card_dict, "tags", []) or [],
        )
