"""Convert Claude conversations into context cards for the FileContextStore.

This builder reads conversations and generates indexed cards that the context
assembly system can discover and select. Each conversation becomes a card with:
- keywords extracted from the conversation text
- a summary of the discussion
- references to the conversation file
- provenance (created from this conversation)

Cards are stored as JSON files in the cards_dir and indexed by FileContextStore.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConversationCardBuilder:
    """Build context cards from Claude conversations."""

    def __init__(self, cards_dir: str, corpus_root: Optional[str] = None):
        """Initialize the builder.

        Args:
            cards_dir: Directory where generated cards are written (JSON files).
            corpus_root: Corpus root for relative path resolution.
        """
        self.cards_dir = Path(cards_dir)
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self.cards_dir.mkdir(parents=True, exist_ok=True)

    def _extract_keywords(self, text: str, limit: int = 10) -> List[str]:
        """Extract keywords from conversation text.

        Looks for:
        - Capitalized words (likely topics/names)
        - Bracketed terms [term]
        - Words after "discuss", "about", "regarding"
        - Repeated significant words
        """
        keywords = set()

        # Find capitalized words (likely topics/entities)
        for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text):
            word = match.group(1).lower()
            if len(word) > 2 and word not in ("user", "assistant"):
                keywords.add(word)

        # Find bracketed terms
        for match in re.finditer(r"\[([^\]]+)\]", text):
            term = match.group(1).lower().strip()
            if len(term) > 2:
                keywords.add(term)

        # Find words after key phrases
        for phrase in ("discuss", "about", "regarding", "focus", "cover", "topic"):
            pattern = f"{phrase}[^.!?]*?([a-z]+(?:\\s+[a-z]+)?)"
            for match in re.finditer(pattern, text, re.IGNORECASE):
                word = match.group(1).lower()
                if len(word) > 2:
                    keywords.add(word)

        # Find frequently occurring words (3+ occurrences)
        words = re.findall(r"\b([a-z]{3,})\b", text.lower())
        word_counts = {}
        for word in words:
            if word not in ("the", "and", "for", "are", "but", "with", "that", "this"):
                word_counts[word] = word_counts.get(word, 0) + 1

        for word, count in sorted(word_counts.items(), key=lambda x: -x[1]):
            if count >= 3 and len(keywords) < limit:
                keywords.add(word)

        return sorted(list(keywords))[:limit]

    def _create_summary(self, conv: Dict[str, Any]) -> str:
        """Create a summary of the conversation.

        Extracts the first user question and key assistant responses.
        """
        messages = conv.get("messages") or conv.get("turns") or []
        if not messages:
            return "Empty conversation"

        parts = []

        # Find first user question
        for msg in messages:
            if msg.get("role") == "user":
                text = (msg.get("text") or msg.get("content") or "")[:100]
                parts.append(f"Discussion: {text}")
                break

        # Count turns
        user_count = sum(1 for m in messages if m.get("role") == "user")
        assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        parts.append(f"{user_count} user questions, {assistant_count} assistant responses")

        # Identify topic from metadata
        if conv.get("rep_name"):
            parts.append(f"Rep: {conv['rep_name']}")

        return " | ".join(parts)

    def build_card(
        self,
        conv_id: str,
        conv: Dict[str, Any],
        conv_file_path: str,
    ) -> Dict[str, Any]:
        """Build a card from a conversation.

        Args:
            conv_id: Conversation identifier (e.g., "docs:claude:design")
            conv: Conversation dict with messages/turns
            conv_file_path: Path to the conversation JSON file

        Returns:
            Card dict conforming to FileContextStore schema
        """
        # Extract metadata
        text = self._conversation_to_text(conv)
        keywords = self._extract_keywords(text)
        summary = self._create_summary(conv)

        # Compute file hash
        try:
            file_hash = hashlib.sha256(str(conv).encode()).hexdigest()
        except Exception:
            file_hash = ""

        # Build card with unified source schema
        card: Dict[str, Any] = {
            "id": conv_id,
            "keywords": keywords,
            "summary": summary,
            "sources": [
                {
                    "type": "conversation",
                    "id": conv_id,
                    "path": conv_file_path,
                    "sha256": file_hash,
                    "turn_count": len(self._get_messages(conv)),
                    "why": "Claude conversation",
                }
            ],
            "conventions": [],
            "provenance": {
                "created_by_task": "conversation_card_builder",
                "model": conv.get("model_hint") or "unknown",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_verified_at": datetime.now(timezone.utc).isoformat(),
            },
            "usage_count": 0,
            "last_outcome": "unknown",
        }

        return card

    @staticmethod
    def _get_messages(conv: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract messages from conversation dict."""
        return conv.get("messages") or conv.get("turns") or []

    @staticmethod
    def _conversation_to_text(conv: Dict[str, Any]) -> str:
        """Convert conversation to plain text for keyword extraction."""
        parts = []
        messages = ConversationCardBuilder._get_messages(conv)
        for msg in messages:
            if isinstance(msg, dict):
                text = msg.get("text") or msg.get("content") or ""
                parts.append(text)
        return " ".join(parts)

    def write_card(self, card: Dict[str, Any]) -> Path:
        """Write a card to disk.

        Returns:
            Path to the written card file
        """
        card_file = self.cards_dir / f"{card['id']}.json"
        card_file.write_text(json.dumps(card, indent=2))
        return card_file

    def build_and_write(
        self,
        conv_id: str,
        conv: Dict[str, Any],
        conv_file_path: str,
    ) -> Optional[Path]:
        """Build a card and write it to disk.

        Returns:
            Path to written card, or None on error
        """
        try:
            card = self.build_card(conv_id, conv, conv_file_path)
            return self.write_card(card)
        except Exception:  # noqa: BLE001
            return None

    def build_all_from_directory(self, conversations_dir: str) -> List[Path]:
        """Build cards for all conversations in a directory.

        Scans for *.json files and generates a card for each.

        Returns:
            List of paths to written cards
        """
        conv_dir = Path(conversations_dir)
        if not conv_dir.is_dir():
            return []

        cards_written = []
        for conv_file in conv_dir.glob("*.json"):
            try:
                conv = json.loads(conv_file.read_text())
                conv_id = conv_file.stem
                rel_path = str(conv_file.relative_to(self.corpus_root)) if self.corpus_root else str(conv_file)
                card_path = self.build_and_write(conv_id, conv, rel_path)
                if card_path:
                    cards_written.append(card_path)
            except (json.JSONDecodeError, OSError):
                pass

        return cards_written
