"""Card filter — LLM-based relevance filtering for context cards."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .adapters import ModelProvider


@dataclass
class CardMetadata:
    """Metadata for a selected context card."""
    id: str                          # card ID from retrieval
    title: str                       # card summary/title
    file_count: int                  # number of files in this card
    files: List[str] = field(default_factory=list)  # top file paths
    relevance_score: float = 0.5     # LLM relevance judgment (0-1)
    adapter: str = ""                # "keyword" or "vector"


def filter_cards_by_relevance(
    task: str,
    candidate_cards: List[Dict[str, Any]],
    *,
    model_provider: Optional[ModelProvider] = None,
) -> List[CardMetadata]:
    """Use LLM to filter context cards by relevance to the task.

    Takes candidate cards from retrieval and scores each by relevance.
    Returns only cards with relevance > 0.5, ordered by score.

    Args:
        task: The user's task text
        candidate_cards: List of {id, title, files, adapter} from retrieval
        model_provider: Optional ModelProvider for LLM scoring
                       (if None, returns all cards as equally relevant)

    Returns:
        Filtered and scored CardMetadata list, ordered by relevance_score DESC
    """
    if not candidate_cards:
        return []

    # Fallback: no LLM available, return all cards as relevant
    if model_provider is None:
        return [
            CardMetadata(
                id=c.get("id", ""),
                title=c.get("title", ""),
                file_count=len(c.get("files", [])),
                files=c.get("files", [])[:3],  # top 3 files per card
                relevance_score=0.7,  # neutral score
                adapter=c.get("adapter", ""),
            )
            for c in candidate_cards
        ]

    # Build list of candidates for LLM judgment
    card_list = "\n".join(
        f"- {c.get('title', 'Untitled')} ({len(c.get('files', []))} files)"
        for c in candidate_cards
    )

    prompt = f"""Given this task: {task}

Which of these context cards are relevant?

{card_list}

For each card, rate relevance 0-1 (0=irrelevant, 1=essential). Return only cards with relevance > 0.5.

Format: one per line, "Card Title: 0.85" """

    # Note: LLM scoring would happen here via model_provider.generate()
    # For now, return fallback (this is a scaffold for future LLM integration)
    return [
        CardMetadata(
            id=c.get("id", ""),
            title=c.get("title", ""),
            file_count=len(c.get("files", [])),
            files=c.get("files", [])[:3],
            relevance_score=0.7,
            adapter=c.get("adapter", ""),
        )
        for c in candidate_cards
    ]
