"""Feedback processor — convert human corrections into guidance cards.

When users correct the AI or provide feedback, this processor:
1. Analyzes the feedback to extract the principle/instruction
2. Determines if it's rep-specific or environment-wide
3. Matches against existing guidance cards (semantic similarity)
4. Creates new cards or updates existing ones
5. Stores via GuidanceCardManager (auto-syncs to all consumers)

Feedback becomes system prompt guidance, not just rep-specific notes.
All guidance is unified in the guidance card system.

Example flows:
- Chat correction → analyzed → "don't do X" principle → guidance card tagged rep:user_id
- Team feedback → analyzed → "always do Y" principle → guidance card (no rep tag)
- Both available to GuidanceProvider.select() for future runs
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager

log = logging.getLogger("quest-ai-runner.feedback")


class FeedbackProcessor:
    """Convert human feedback/corrections into guidance cards.

    Analyzes incoming feedback, extracts principles, and stores them as
    guidance cards with appropriate tags (rep_id, source, task_type, etc.).
    Intelligently decides whether to create new cards or update existing ones.
    """

    def __init__(self, guidance_manager: GuidanceCardManager):
        """Initialize with a guidance card manager.

        Args:
            guidance_manager: GuidanceCardManager instance for storing cards.
        """
        self.manager = guidance_manager

    def process_feedback(
        self,
        feedback_text: str,
        *,
        rep_id: Optional[str] = None,
        source: str = "feedback",
        task_type: Optional[str] = None,
        message_id: Optional[str] = None,
        context: Optional[str] = None,
    ) -> Optional[str]:
        """Process human feedback and create/update guidance card.

        Args:
            feedback_text: The human's feedback/correction.
            rep_id: If provided, marks guidance as rep-specific (rep:rep_id tag).
            source: Source of feedback (feedback, correction, instruction, etc.).
            task_type: Type of task this feedback relates to (plan, answer, deep, etc.).
            message_id: Optional reference to the chat message being corrected.
            context: Optional additional context about the feedback.

        Returns:
            Card ID of created/updated card, or None if processing failed.
        """
        if not feedback_text or not feedback_text.strip():
            return None

        try:
            # Step 1: Analyze feedback to extract principle
            principle = self._extract_principle(feedback_text)
            if not principle:
                log.warning(f"Could not extract principle from feedback: {feedback_text[:100]}")
                return None

            # Step 2: Generate card title and body
            title, body = self._generate_card_content(principle, feedback_text, context)

            # Step 3: Determine tags
            tags = self._determine_tags(rep_id, source, task_type)

            # Step 4: Generate stable card ID (deterministic from principle)
            card_id = self._generate_card_id(principle, rep_id)

            # Step 5: Check if card exists (match by semantic similarity)
            existing_card = self._find_matching_card(principle)

            if existing_card:
                # Update existing card: append feedback as new version
                updated_body = self._merge_card_content(
                    existing_card.body, body, feedback_text, rep_id
                )
                self.manager.save_card(
                    card_id,
                    title,
                    updated_body,
                    description=f"Learned from {source}",
                    tags=tags,
                    feedback_source=message_id or source,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                log.info(f"Updated guidance card {card_id} from {source}")
            else:
                # Create new card
                self.manager.save_card(
                    card_id,
                    title,
                    body,
                    description=f"Learned from {source}",
                    tags=tags,
                    feedback_source=message_id or source,
                    created_from=feedback_text[:200],
                )
                log.info(f"Created guidance card {card_id} from {source}")

            return card_id
        except Exception as e:  # noqa: BLE001
            log.error(f"Error processing feedback: {e}")
            return None

    def _extract_principle(self, feedback_text: str) -> Optional[str]:
        """Extract the core principle from feedback text.

        Simple heuristic-based extraction. Could be enhanced with LLM analysis.
        Looks for imperative statements (do X, don't Y) or key instructions.
        """
        text = feedback_text.strip().lower()

        # Look for common patterns
        patterns = [
            ("don't", "avoid", "never"),
            ("always", "must", "should"),
            ("never say", "avoid saying"),
            ("remember", "note that"),
        ]

        for pattern_set in patterns:
            for pattern in pattern_set:
                if pattern in text:
                    # Return the sentence containing the pattern
                    for sentence in feedback_text.split("."):
                        if pattern.lower() in sentence.lower():
                            return sentence.strip()

        # Fallback: return first 100 chars as principle
        if len(feedback_text) > 10:
            return feedback_text[:100].strip()

        return None

    def _generate_card_content(
        self, principle: str, feedback_text: str, context: Optional[str] = None
    ) -> tuple[str, str]:
        """Generate card title and body from principle and feedback."""
        # Simple title generation (first 50 chars of principle)
        title = principle[:50].rstrip(",.!?") + ("..." if len(principle) > 50 else "")

        # Body: principle + original feedback + context
        body_parts = [
            f"**Principle**: {principle}",
            "",
            "**Feedback**:",
            f"> {feedback_text}",
        ]

        if context:
            body_parts.extend(
                [
                    "",
                    "**Context**:",
                    f"> {context}",
                ]
            )

        body_parts.append(
            "\n*Learned from human feedback — apply consistently.*"
        )

        return title, "\n".join(body_parts)

    def _determine_tags(
        self,
        rep_id: Optional[str],
        source: str,
        task_type: Optional[str],
    ) -> List[str]:
        """Determine tags for the guidance card."""
        tags = [f"source:{source}"]

        if rep_id:
            tags.append(f"rep:{rep_id}")

        if task_type:
            tags.append(f"task:{task_type}")

        # Auto-tag as learned feedback
        tags.append("learned")
        tags.append("feedback")

        return tags

    def _generate_card_id(self, principle: str, rep_id: Optional[str]) -> str:
        """Generate a stable, deterministic card ID from principle.

        Format: feedback_<hash> or feedback_rep_<rep_id>_<hash>
        """
        principle_hash = hashlib.sha256(principle.encode()).hexdigest()[:8]

        if rep_id:
            return f"feedback_{rep_id}_{principle_hash}"
        return f"feedback_{principle_hash}"

    def _find_matching_card(self, principle: str) -> Optional[Any]:
        """Find existing guidance card matching this principle (semantic).

        Simple keyword overlap; could be enhanced with vector similarity.
        """
        cards = self.manager.load_cards()
        if not cards:
            return None

        principle_words = set(principle.lower().split())
        best_match = None
        best_score = 0

        for card in cards:
            # Skip non-feedback cards
            if "feedback" not in (card.tags or []):
                continue

            card_text = f"{card.title} {card.description} {card.body}".lower()
            card_words = set(card_text.split())

            overlap = len(principle_words & card_words)
            if overlap > best_score and overlap > 2:  # Require at least 3 word matches
                best_score = overlap
                best_match = card

        return best_match if best_score > 0 else None

    def _merge_card_content(
        self,
        existing_body: str,
        new_body: str,
        original_feedback: str,
        rep_id: Optional[str],
    ) -> str:
        """Merge new feedback into existing card body."""
        # Keep existing content, append new feedback as version
        rep_tag = f" ({rep_id})" if rep_id else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        merged = f"""{existing_body}

---

**Update {timestamp}{rep_tag}**:
> {original_feedback}"""

        return merged

    def process_rep_correction(
        self,
        user_id: str,
        correction_text: str,
        message_id: Optional[str] = None,
    ) -> Optional[str]:
        """Convenience method: process a rep correction directly.

        Args:
            user_id: The rep being corrected.
            correction_text: The correction/feedback.
            message_id: Optional chat message reference.

        Returns:
            Card ID of created/updated guidance card.
        """
        return self.process_feedback(
            correction_text,
            rep_id=user_id,
            source="correction",
            message_id=message_id,
        )
