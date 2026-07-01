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
        provider: Optional[Any] = None,
        model: Optional[str] = None,
    ):
        """Initialize with file-based and optional dynamic guidance sources.

        Args:
            cards_dir: Path to guidance cards directory. If None, auto-resolves.
            dynamic_guidance_loader: Optional callable that returns dynamic guidance
                                    dicts (e.g., from rep profiles, feedback DB).
                                    Called each select() to pick up new feedback.
            vector_store: Optional vector store (e.g., Qdrant) for semantic search.
                         When set, guidance cards are indexed for similarity matching.
            provider: ModelProvider (MultiProvider) for LLM relevance filtering of
                      candidate guidance cards after tag/keyword/semantic scoring.
            model: Resolved model ID to use for filtering (e.g. balanced tier).
        """
        from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager

        self.manager = GuidanceCardManager(cards_dir=cards_dir)
        self.dynamic_loader = dynamic_guidance_loader
        self.vector_store = vector_store
        self._provider = provider
        self._model = model
        self._cards_cache: List[CoreGuidanceCard] = []
        self._dynamic_cache: List[Dict[str, Any]] = []
        self._cache_valid = False

    def select(
        self,
        user_message: str = "",
        *,
        task_type: Optional[str] = None,
        rep_id: Optional[str] = None,
        team_id: Optional[str] = None,
        org_id: Optional[str] = None,
        operation: Optional[str] = None,
        function_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[CoreGuidanceCard]:
        """Intelligently select applicable guidance with hierarchical scope resolution.

        Selection factors (in priority order):
        1. Scope hierarchy: rep → team → org → global (more specific overrides general)
        2. Operation-type matching (operation:plan, operation:answer, operation:deep, etc.)
        3. Function-level guidance (function:list_goals, function:create_quest, etc.)
        4. Task-type matching (task:*)
        5. Semantic similarity to user message (if vector store available)
        6. Keyword matches in title/description

        Args:
            user_message: The user's input (for semantic/keyword matching).
            task_type: Type of task (plan, answer, deep, confirm, etc.).
            rep_id: The AI rep running this task (rep-specific guidance).
            team_id: The team context (team-level guidance applies).
            org_id: The organization context (org-level guidance applies).
            operation: Type of operation (plan, answer, read, write, query, etc.).
            function_name: Specific function being called (list_goals, create_quest, etc.).
            tags: Additional tags to match (custom categories).
            limit: Max cards to return.

        Returns:
            List of applicable GuidanceCard objects, ordered by relevance and scope.
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
            scope_level = -1  # Tracks which scope matched (higher = more specific)

            card_tags = set(card.tags or [])

            # Factor 1: Hierarchical scope matching (highest priority)
            # More specific scopes override general ones
            if f"scope:rep:{rep_id}" in card_tags:
                score += 200
                scope_level = 4
                reasons.append(f"scope:rep:{rep_id}")
            elif f"scope:team:{team_id}" in card_tags:
                score += 150
                scope_level = 3
                reasons.append(f"scope:team:{team_id}")
            elif f"scope:org:{org_id}" in card_tags:
                score += 100
                scope_level = 2
                reasons.append(f"scope:org:{org_id}")
            elif "scope:global" in card_tags:
                score += 50
                scope_level = 1
                reasons.append("scope:global")
            else:
                # Cards without explicit scope apply globally
                score += 40
                scope_level = 0
                reasons.append("scope:implicit-global")

            # Factor 2: Operation-type matching
            if operation:
                if f"operation:{operation}" in card_tags:
                    score += 120
                    reasons.append(f"operation:{operation}")

                # Sub-operation matching (e.g., operation:write when doing planning)
                operation_family = self._get_operation_family(operation)
                if operation_family and f"operation:{operation_family}" in card_tags:
                    score += 60
                    reasons.append(f"operation:{operation_family}")

            # Factor 3: Function-level guidance
            if function_name:
                if f"function:{function_name}" in card_tags:
                    score += 110
                    reasons.append(f"function:{function_name}")

            # Factor 4: Task-type matching
            if task_type and f"task:{task_type}" in card_tags:
                score += 80
                reasons.append(f"task:{task_type}")

            # Factor 5: Custom tags
            if tags:
                for tag in tags:
                    if tag in card_tags:
                        score += 40
                        reasons.append(f"tag:{tag}")

            # Factor 6: Semantic similarity (if vector store available)
            if self.vector_store and user_message:
                try:
                    similarity = self._semantic_score(card, user_message)
                    score += int(similarity * 70)
                    if similarity > 0.5:
                        reasons.append(f"semantic:{similarity:.2f}")
                except Exception:  # noqa: BLE001
                    pass

            # Factor 7: Keyword matching
            if user_message:
                keyword_score = self._keyword_score(card, user_message)
                score += keyword_score
                if keyword_score > 0:
                    reasons.append(f"keyword:{keyword_score}")

            # Include card if it has any relevance
            if score > 0:
                scored.append((score, scope_level, reasons, card))

        # Sort by: scope (specific first), then score (highest first)
        scored.sort(key=lambda x: (-x[1], -x[0]))
        # Take 2x limit as candidates so LLM filter has headroom
        candidates = [card for _, _, _, card in scored[: limit * 2]]

        # LLM filter: verify candidates are genuinely relevant to user_message
        if self._provider is not None and user_message and candidates:
            try:
                from .card_filter import filter_cards_by_relevance
                candidate_dicts = [
                    {
                        "id": c.id,
                        "title": f"{c.title} — {c.relevance}" if c.relevance else c.title,
                        "files": [],
                        "adapter": "guidance",
                    }
                    for c in candidates
                ]
                kept = filter_cards_by_relevance(
                    user_message, candidate_dicts,
                    model_provider=self._provider, model=self._model,
                )
                kept_ids = {m.id for m in kept}
                candidates = [c for c in candidates if c.id in kept_ids]
            except Exception:
                pass  # fall back to tag/keyword ranking

        result = candidates[:limit]

        # Log selections for debugging
        if result:
            log.debug(
                f"Selected {len(result)} guidance cards "
                f"(task={task_type}, rep={rep_id}, team={team_id}, org={org_id}, "
                f"operation={operation}, function={function_name}): "
                f"{[c.id for c in result]}"
            )

        return result

    def list(self) -> List[CoreGuidanceCard]:
        """List all available guidance cards (file-based + dynamic)."""
        self._reload_if_needed()
        dynamic_cards = [self._to_core_card(d) for d in self._dynamic_cache]
        return self._cards_cache + dynamic_cards

    def read(self, card_id: str) -> Optional[CoreGuidanceCard]:
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
            searchable = f"{card.title} {card.relevance} {card.body}"
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

        searchable = f"{card.title} {card.relevance}".lower()
        card_words = set(searchable.split())
        matches = len(query_words & card_words)
        return matches * 10  # Each match worth 10 points

    @staticmethod
    def _get_operation_family(operation: str) -> Optional[str]:
        """Get the operation family for broader matching.

        Maps specific operations to families for guidance inheritance:
        - plan → planning (for plan-related operations)
        - answer → answering (for answer-related operations)
        - read, query → retrieval (for data access operations)
        - write, update, create, delete → mutation (for write operations)
        - deep → autonomous (for deep work operations)
        """
        families = {
            "plan": "planning",
            "planning": "planning",
            "answer": "answering",
            "answering": "answering",
            "read": "retrieval",
            "query": "retrieval",
            "retrieval": "retrieval",
            "write": "mutation",
            "update": "mutation",
            "create": "mutation",
            "delete": "mutation",
            "mutation": "mutation",
            "deep": "autonomous",
            "autonomous": "autonomous",
            "confirm": "confirmation",
            "confirmation": "confirmation",
        }
        return families.get(operation.lower())

    @staticmethod
    def _to_core_card(card_dict: Any) -> CoreGuidanceCard:
        """Convert card dict or object to core GuidanceCard."""
        if isinstance(card_dict, dict):
            return CoreGuidanceCard(
                id=card_dict.get("id", "unknown"),
                title=card_dict.get("title", ""),
                body=card_dict.get("body", ""),
                relevance=card_dict.get("relevance", card_dict.get("description", "")),
                tags=list(card_dict.get("tags") or []),
            )
        # Assume it's an object with attributes
        return CoreGuidanceCard(
            id=getattr(card_dict, "id", "unknown"),
            title=getattr(card_dict, "title", ""),
            body=getattr(card_dict, "body", ""),
            relevance=getattr(card_dict, "relevance", getattr(card_dict, "description", "")),
            tags=list(getattr(card_dict, "tags", None) or []),
        )
