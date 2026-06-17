"""Card filter — LLM-based relevance filtering for context cards."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .adapters import ModelProvider

_log = logging.getLogger("quest-ai-runner.card-filter")


@dataclass
class CardMetadata:
    """Metadata for a selected context card.

    The ``files`` list is ordered by relevance to the task — the LLM ranks
    individual files within the card so the UI can show the most relevant first,
    and paginate through if needed.
    """
    id: str                          # card ID from retrieval
    title: str                       # card summary/title
    file_count: int                  # number of files in this card
    files: List[str] = field(default_factory=list)  # top file paths, ordered by relevance
    relevance_score: float = 0.5     # LLM relevance judgment (0-1)
    adapter: str = ""                # "keyword" or "vector"


def filter_cards_by_relevance(
    task: str,
    candidate_cards: List[Dict[str, Any]],
    *,
    model_provider: Optional[ModelProvider] = None,
) -> List[CardMetadata]:
    """Use LLM to filter context cards by relevance to the task.

    Two-stage filtering:
    1. Card-level: score each card 0-1 by relevance to the task
    2. File-level: within each selected card, rank files by relevance

    Returns only cards with relevance > 0.5, ordered by score DESC.
    Files within each card are ordered by relevance DESC.

    Args:
        task: The user's task text
        candidate_cards: List of {id, title, files, adapter} from retrieval
        model_provider: Optional ModelProvider for LLM scoring
                       (if None, returns all cards with files in original order)

    Returns:
        Filtered and scored CardMetadata list, ordered by relevance_score DESC
    """
    if not candidate_cards:
        return []

    # Fallback: no LLM available, return all cards with top 3 files
    if model_provider is None:
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

    # --- Stage 1: Card-level relevance scoring ---
    card_list = "\n".join(
        f"[{c.get('id', '?')}] {c.get('title', 'Untitled')} ({len(c.get('files', []))} files)"
        for c in candidate_cards
    )

    card_prompt = f"""You are a code/context relevance expert. Given a task, score which context cards are relevant.

TASK: {task}

CANDIDATE CARDS:
{card_list}

For each card, decide: is it relevant to the task? Rate 0-1 where:
- 0 = irrelevant (different domain, no overlap)
- 0.5 = potentially relevant (partial match, might help)
- 1 = essential (directly addresses the task)

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "cards": [
    {{"id": "card-id", "score": 0.85}},
    ...
  ]
}}

Return ONLY cards with score >= 0.5."""

    try:
        card_scores_json = model_provider.answer(
            [{"role": "user", "content": card_prompt}],
            model=model_provider.list_models()[0] if model_provider.list_models() else None
        )
        card_scores_raw = json.loads(card_scores_json or "{}")
        card_scores = {c["id"]: c["score"] for c in (card_scores_raw.get("cards") or [])}
    except Exception as e:
        _log.debug("card-level scoring failed, falling back: %s", e)
        # Fallback: neutral scores for all
        card_scores = {c.get("id", ""): 0.7 for c in candidate_cards}

    # --- Stage 2: File-level relevance ranking (within selected cards) ---
    results = []
    for card in candidate_cards:
        card_id = card.get("id", "")
        score = card_scores.get(card_id, 0)
        if score < 0.5:
            continue  # Skip irrelevant cards

        card_files = card.get("files", [])
        if not card_files:
            # Card has no files, add it with empty file list
            results.append(
                CardMetadata(
                    id=card_id,
                    title=card.get("title", ""),
                    file_count=0,
                    files=[],
                    relevance_score=score,
                    adapter=card.get("adapter", ""),
                )
            )
            continue

        # Rank files within this card by relevance to task
        file_list = "\n".join(f"- {f}" for f in card_files)
        file_prompt = f"""You are a code relevance expert. Given a task and a list of files within a context card, rank which files are most relevant.

TASK: {task}

CARD: {card_id} - {card.get('title', 'Untitled')}

FILES IN THIS CARD:
{file_list}

Rank these files by relevance (most to least). Score each 0-1 where:
- 1 = essential for this task
- 0.5 = might be useful
- 0 = not relevant to task

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "files": [
    {{"path": "path/to/file.py", "score": 0.95}},
    ...
  ]
}}"""

        try:
            file_scores_json = model_provider.answer(
                [{"role": "user", "content": file_prompt}],
                model=model_provider.list_models()[0] if model_provider.list_models() else None
            )
            file_scores_raw = json.loads(file_scores_json or "{}")
            # Build path -> score map from response
            file_scores_map = {}
            for f in (file_scores_raw.get("files") or []):
                path = f.get("path", "")
                score = f.get("score", 0.5)
                file_scores_map[path] = score
            # Sort files by score descending, take top 5
            ranked_files = sorted(
                card_files,
                key=lambda f: file_scores_map.get(f, 0),
                reverse=True
            )[:5]
        except Exception as e:
            _log.debug("file-level scoring failed for card %s, using original order: %s", card_id, e)
            # Fallback: use original order, top 5
            ranked_files = card_files[:5]

        results.append(
            CardMetadata(
                id=card_id,
                title=card.get("title", ""),
                file_count=len(card_files),
                files=ranked_files,
                relevance_score=score,
                adapter=card.get("adapter", ""),
            )
        )

    # Sort results by relevance score descending
    results.sort(key=lambda r: r.relevance_score, reverse=True)
    return results
