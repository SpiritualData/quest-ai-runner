"""Generate LLM-based metadata (title, description, keywords) for context cards.

Handles the chicken-and-egg problem:
- Card is created with initial sources, LLM generates metadata
- More sources are added to the card later
- Metadata usually stays valid, but can be refreshed if needed

Cards track when metadata was last generated and can be refreshed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol


class ModelProvider(Protocol):
    """Minimal interface for LLM calls."""

    def answer(
        self, messages: List[Dict[str, str]], *, model: str, system: Optional[str] = None
    ) -> str:
        """Generate text from messages."""
        ...


class CardMetadataGenerator:
    """Generate or refresh metadata for any context card using an LLM.

    A card can have multiple sources (files, conversations, tests, chat history, etc.).
    This generator creates unified metadata (title, description, keywords) for the
    card as a whole, not per-source.

    Metadata can be refreshed later if new sources are added.
    """

    def __init__(self, model_provider: ModelProvider, model: str = "fast"):
        """Initialize with an LLM provider.

        Args:
            model_provider: ModelProvider for LLM calls (must have .answer() method)
            model: resolved model id (from registry.resolve_tier) to use for metadata gen.
                   Callers should pass registry.resolve_tier("fast") — never a pinned id.
        """
        self.model_provider = model_provider
        self._model = model

    def generate(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """Generate metadata for a card based on its sources.

        Args:
            card: Card dict with 'sources' array (each source has type, path/id, etc.)

        Returns:
            Metadata dict with 'title', 'description', 'keywords'
        """
        # Summarize all sources for the LLM
        source_summaries = self._summarize_sources(card.get("sources", []))

        prompt = f"""Given these knowledge sources that belong together in one card, generate metadata:

SOURCES:
{source_summaries}

Generate a JSON response with:
- title: 1-3 words describing the unified topic (e.g., "JWT Authentication", "Error Handling")
- description: 1-2 sentences summarizing what this card covers and its purpose
- keywords: list of 4-8 key topics for discovery (e.g., ["jwt", "authentication", "security", "api"])

Return ONLY valid JSON, no other text."""

        try:
            response = self.model_provider.answer(
                [{"role": "user", "content": prompt}],
                model=self._model,
            )
            metadata = json.loads(response)
            return {
                "title": metadata.get("title", "Topic"),
                "description": metadata.get("description", ""),
                "keywords": metadata.get("keywords", []),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:  # noqa: BLE001
            # Fallback if LLM fails
            return self._generate_fallback_metadata(card)

    def refresh(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh metadata for an existing card (e.g., after new sources added).

        Same as generate() but also updates the card's provenance.

        Args:
            card: Existing card dict

        Returns:
            Updated metadata dict
        """
        metadata = self.generate(card)
        # Track that metadata was refreshed
        if "provenance" in card:
            card["provenance"]["metadata_refreshed_at"] = metadata["generated_at"]
        return metadata

    @staticmethod
    def _summarize_sources(sources: List[Dict[str, Any]]) -> str:
        """Create a text summary of all sources for the LLM."""
        if not sources:
            return "(no sources)"

        summaries = []
        for src in sources:
            src_type = src.get("type", "unknown")
            if src_type == "file":
                path = src.get("path", "?")
                why = src.get("why", "")
                summaries.append(f"- FILE: {path} ({why})")
            elif src_type == "conversation":
                conv_id = src.get("id", "?")
                turns = src.get("turn_count", "?")
                why = src.get("why", "")
                summaries.append(f"- CONVERSATION: {conv_id} ({turns} turns, {why})")
            elif src_type == "chat_history":
                session = src.get("session_id", "?")
                msgs = src.get("message_count", "?")
                why = src.get("why", "")
                summaries.append(f"- CHAT HISTORY: {session} ({msgs} messages, {why})")
            elif src_type == "test":
                path = src.get("path", "?")
                count = src.get("test_count", "?")
                why = src.get("why", "")
                summaries.append(f"- TEST: {path} ({count} tests, {why})")
            elif src_type == "quest_doc":
                url = src.get("url", "?")
                title = src.get("title", "?")
                summaries.append(f"- QUEST DOC: {title} ({url})")
            else:
                summaries.append(f"- {src_type.upper()}: {src.get('path', src.get('id', '?'))}")

        return "\n".join(summaries)

    @staticmethod
    def _generate_fallback_metadata(card: Dict[str, Any]) -> Dict[str, Any]:
        """Generate basic metadata without LLM (fallback if LLM fails).

        Very simple: use first source as topic hint.
        """
        sources = card.get("sources", [])
        title = "Topic"
        description = f"Card with {len(sources)} sources"
        keywords = []

        if sources:
            first = sources[0]
            src_type = first.get("type", "unknown")
            if src_type == "file":
                title = first.get("path", "Topic").split("/")[-1].split(".")[0].title()
            elif src_type == "conversation":
                title = first.get("id", "Topic").replace("_", " ").title()
            elif src_type == "test":
                title = "Tests"
            keywords = [src_type]

        return {
            "title": title,
            "description": description,
            "keywords": keywords,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
