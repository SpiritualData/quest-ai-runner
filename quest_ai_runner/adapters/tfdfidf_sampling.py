"""TF-DF-IDF sampling utilities for context bootstrapping and retrieval.

Provides reusable functions for selecting representative items (files, conversations, etc.)
from a larger corpus using TF-DF-IDF heuristic: distinctive within their group, penalizing
corpus-wide generic terms. Used by FileContextStore, ClaudeConversationsAdapter, etc.

TF-DF-IDF = tf(t,d) × df(t,cluster) × idf(t,corpus) after Schilder & Kondadadi (2008):
- tf: binary term presence in a document (1 or 0)
- cluster_df: how many documents in the SAME group contain the term
  (high = term is representative of the group's topic)
- idf: log((N+1)/(n_t+1))+1, where N = total corpus items, n_t = items containing term
  (high = term is rare globally → distinctive)

Files with terms shared across their folder (cluster_df) AND rare in the full corpus (idf)
score highest — they are the best representatives of their folder's topic.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Dict, List, Optional, Set


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

    Groups items and for each group selects top-K items scored by TF-DF-IDF:
    terms shared with group-mates (cluster_df) AND rare globally (idf) score highest.

    Args:
        items: List of items to sample from.
        get_terms: Function that extracts distinctive terms from an item.
        samples_per_group: Number of items to select per group (default 3).
        get_group: Function that returns a group key for an item. If None,
                   all items are treated as one group.
        get_score_boost: Optional function that returns a multiplicative score
                        boost for an item (e.g., recency). Default 1.0.

    Returns:
        Subset of items, ordered by group then by descending TF-DF-IDF score.
    """
    if not items:
        return []

    if get_group is None:
        get_group = lambda x: "all"

    all_terms_per_item = {item: get_terms(item) for item in items}
    groups: Dict[str, List[str]] = {}
    for item in items:
        groups.setdefault(get_group(item), []).append(item)

    N_items = len(items)

    # corpus_df: how many items contain each term (denominator for global IDF)
    corpus_df: Dict[str, int] = {}
    for terms in all_terms_per_item.values():
        for term in terms:
            corpus_df[term] = corpus_df.get(term, 0) + 1

    # group_df: per-group term counts (cluster DF — the "DF" in TF-DF-IDF)
    group_df: Dict[str, Dict[str, int]] = {}
    for item in items:
        gdf = group_df.setdefault(get_group(item), {})
        for term in all_terms_per_item[item]:
            gdf[term] = gdf.get(term, 0) + 1

    representatives: List[str] = []
    for group_key in sorted(groups.keys()):
        items_in_group = groups[group_key]

        if len(items_in_group) <= samples_per_group:
            representatives.extend(items_in_group)
            continue

        gdf = group_df.get(group_key, {})
        scored: List[tuple] = []
        for item in items_in_group:
            terms = all_terms_per_item[item]
            if not terms:
                continue

            score = 0.0
            for term in terms:
                cluster_df = gdf.get(term, 1)
                n_t = corpus_df.get(term, 1)
                idf = math.log((N_items + 1) / (n_t + 1)) + 1.0
                score += cluster_df * idf  # TF=1 (binary), so tf × cluster_df × idf = cluster_df × idf

            boost = get_score_boost(item) if get_score_boost else 1.0
            scored.append((score * boost, item))

        if scored:
            scored.sort(reverse=True, key=lambda x: x[0])
            representatives.extend([item for _, item in scored[:samples_per_group]])

    return representatives
