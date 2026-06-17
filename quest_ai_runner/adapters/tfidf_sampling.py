"""TF-DF-IDF sampling utilities for context bootstrapping and retrieval.

Provides reusable functions for selecting representative items (files, conversations, etc.)
from a larger corpus using TF-DF-IDF heuristic: distinctive within their group, penalizing
corpus-wide generic terms. Used by FileContextStore, ClaudeConversationsAdapter, etc.

Reference: Schilder & Kondadadi (2008) on multi-document summarization.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional, Set


def extract_terms(text: str, stopwords: Optional[Set[str]] = None) -> set:
    """Extract distinctive terms from text.

    Splits on whitespace and punctuation, filters out short parts and stopwords.
    Returns terms likely to be distinctive to the text's topic.

    Args:
        text: Text to extract terms from (filename, digest, etc.).
        stopwords: Set of words to exclude. If None, uses a minimal default set.
    """
    if stopwords is None:
        stopwords = {
            "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
            "be", "been", "being", "have", "has", "had", "do", "does", "did",
            "src", "lib", "app", "utils", "test", "spec", "config", "index", "main",
            "start", "recent", "ok", "thanks", "yes", "no", "please", "help",
        }

    terms: set = set()
    # Split on whitespace and punctuation
    for part in re.split(r"[\s\W/\\.]+ ", text.lower()):
        if part and len(part) > 2 and part not in stopwords:
            terms.add(part)
    return terms


def select_representatives(
    items: List[str],
    get_terms: Callable[[str], set],
    samples_per_group: int = 3,
    get_group: Optional[Callable[[str], str]] = None,
    get_score_boost: Optional[Callable[[str], float]] = None,
) -> List[str]:
    """Select representative items from a corpus using TF-DF-IDF heuristic.

    Groups items (optionally) and for each group selects top-K items whose
    terms have the highest TF-DF-IDF scores (distinctive within group,
    penalizing corpus-wide generic terms).

    Args:
        items: List of items to sample from.
        get_terms: Function that extracts distinctive terms from an item.
        samples_per_group: Number of items to select per group (default 3).
        get_group: Function that returns a group key for an item. If None,
                   all items are treated as one group.
        get_score_boost: Optional function that returns a multiplicative score
                        boost for an item (e.g., recency boost). Default 1.0.

    Returns:
        Subset of items, ordered by group then by descending TF-DF-IDF score.
    """
    if not items:
        return []

    # Use identity grouping if no grouper provided
    if get_group is None:
        get_group = lambda x: "all"

    # Extract terms and group assignments
    all_terms_per_item = {item: get_terms(item) for item in items}
    groups = {}
    for item in items:
        group_key = get_group(item)
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(item)

    # Calculate corpus-wide term frequency
    corpus_df: Dict[str, int] = {}
    for terms in all_terms_per_item.values():
        for term in terms:
            corpus_df[term] = corpus_df.get(term, 0) + 1

    total_groups = len(groups)

    # For each group, score items by TF-DF-IDF and select top K
    representatives: List[str] = []
    for group_key in sorted(groups.keys()):
        items_in_group = groups[group_key]

        # If group is small, keep all items
        if len(items_in_group) <= samples_per_group:
            representatives.extend(items_in_group)
            continue

        # TF-DF-IDF: term frequency * (1 + log(total_groups / corpus_df))
        scored: List[tuple] = []
        for item in items_in_group:
            terms = all_terms_per_item[item]
            if not terms:
                continue

            # Sum TF-DF-IDF over all terms in this item
            tf_df_idf_sum = 0.0
            for term in terms:
                tf = 1  # Each term counts once per item in our model
                df = corpus_df.get(term, 1)
                idf = 1.0 + math.log((total_groups + 1) / (df + 1))
                tf_df_idf_sum += tf * idf

            # Apply optional score boost (e.g., recency)
            boost = get_score_boost(item) if get_score_boost else 1.0
            scored.append((tf_df_idf_sum * boost, item))

        # Sort by score descending, take top K
        if scored:
            scored.sort(reverse=True, key=lambda x: x[0])
            representatives.extend([item for _, item in scored[:samples_per_group]])

    return representatives
