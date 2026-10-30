"""Collapsing near-duplicate topic cards: keyword clusters, grouping prompts and fallbacks.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging

import json
from typing import Any, Dict, List, Optional, Set

from hubofhubs.recall.context_bootstrap_support import _extract_json_array, _jaccard, _merge_card_group
from hubofhubs.recall.context_card_ceilings import collapse_cards_by_file_set

_log = logging.getLogger("quest-ai-runner.context")


_DEDUP_MIN_SHARED_KEYWORDS = 2


# Auto-merge Jaccard threshold for the no-LLM fallback (lower than the old 0.7 so the keyword-
# only path still collapses obvious duplicates when no provider is available to judge).
_DEDUP_FALLBACK_JACCARD = 0.30


# Prompt for stage 3: given a cluster of possibly-overlapping cards, ask the LLM which to merge.
_DEDUP_PROMPT = """These topic cards may overlap. Decide which describe the same concept and \
should be merged.

{card_lines}

Return a JSON array of arrays of 1-based card numbers to group together.
Cards in the same inner array will be merged. Each card must appear exactly once.
Example: [[1,2],[3]] merges cards 1+2, keeps 3 separate.
Return ONLY the JSON array, no prose."""


class _UnionFind:
    """Tiny union-find over integer indices for transitive keyword clustering."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self) -> List[List[int]]:
        groups: Dict[int, List[int]] = {}
        for i in range(len(self._parent)):
            groups.setdefault(self.find(i), []).append(i)
        return list(groups.values())


def _card_keyword_set(card: Dict[str, Any]) -> Set[str]:
    """Lowercased keyword set for a card (used to find shared-keyword candidates)."""
    return {str(k).strip().lower() for k in card.get("keywords", []) if str(k).strip()}


def _keyword_clusters(cards: List[Dict[str, Any]]) -> List[List[int]]:
    """Cluster card indices that share >= _DEDUP_MIN_SHARED_KEYWORDS keywords (transitive).

    Uses union-find so a chain A~B~C clusters together even if A and C don't directly overlap.
    Returns a list of index lists (singletons included).
    """
    n = len(cards)
    uf = _UnionFind(n)
    kw_sets = [_card_keyword_set(c) for c in cards]
    for i in range(n):
        for j in range(i + 1, n):
            if len(kw_sets[i] & kw_sets[j]) >= _DEDUP_MIN_SHARED_KEYWORDS:
                uf.union(i, j)
    return uf.clusters()


# Openers of an agent PREAMBLE: the instruction scaffolding a run is wrapped in, which is
# identical across every run and says nothing about what any particular card is for.
_PREAMBLE_OPENERS = (
    "act as ", "you are ", "user's request", "quest outcome:", "scope:", "task:",
    "system:", "role:", "context:", "instructions:",
)


def _is_prompt_echo(summary: str) -> bool:
    """Whether a summary is the task PROMPT rather than a description of the card.

    A card's summary is meant to say what the card is about. ``record()`` had nothing better to
    hand and used the first 200 characters of the task text, which for a persona run is pure
    scaffolding: "Act as Batman. Quest outcome: ... Scope: ...". Every such card carries the same
    words, so the field does no work at all -- it cannot distinguish two cards, and it cannot help
    retrieval pick between them.
    """
    head = (summary or "").strip().lower()
    return any(head.startswith(opener) for opener in _PREAMBLE_OPENERS)


def _first_substantive_line(text: Optional[str]) -> str:
    """The first line of ``text`` that is not agent preamble. "" when there is none."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and not _is_prompt_echo(stripped):
            return stripped
    return ""


def _record_summary(task_text: str, outcome: Dict[str, Any], name: str = "") -> str:
    """The summary for a run-recorded card, preferring what HAPPENED over what was asked.

    Ordered by how much each source actually distinguishes this card from the next one:
    the run's own result first (it describes the work), then the task text with its preamble
    skipped, then the card's name. The raw prompt is never used as-is.
    """
    for candidate in (outcome.get("response"), outcome.get("summary"), outcome.get("result")):
        line = _first_substantive_line(candidate)
        if line:
            return line[:200]
    line = _first_substantive_line(task_text)
    if line:
        return line[:200]
    return (name or "").strip()[:200]


def _parse_groupings(raw: str, n_cards: int) -> Optional[List[List[int]]]:
    """Parse an LLM dedup response into 0-based index groups, or None if invalid.

    Expects a JSON array of arrays of 1-based card numbers covering each card exactly once.
    Returns None (caller keeps the cluster unchanged) on any parse/validation failure — this is
    what keeps a mocked provider that returns non-grouping JSON from corrupting the cards.
    """
    payload = _extract_json_array(raw or "")
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    groups: List[List[int]] = []
    seen: Set[int] = set()
    for inner in parsed:
        if not isinstance(inner, list) or not inner:
            return None
        idxs: List[int] = []
        for num in inner:
            if not isinstance(num, int) or isinstance(num, bool):
                return None
            idx = num - 1  # 1-based -> 0-based
            if idx < 0 or idx >= n_cards or idx in seen:
                return None
            seen.add(idx)
            idxs.append(idx)
        groups.append(idxs)
    # Every card must appear exactly once.
    if len(seen) != n_cards:
        return None
    return groups


def _dedup_cluster_llm(cluster: List[Dict[str, Any]], provider, model) -> List[Dict[str, Any]]:
    """Make ONE LLM call to decide merges within a candidate cluster. Never raises.

    On any failure (no provider, bad response, parse failure) returns the cluster unchanged.
    """
    if provider is None or len(cluster) < 2:
        return cluster
    try:
        card_lines = "\n".join(
            'Card {n}: "{name}" — keywords: [{kws}] — {fc} files'.format(
                n=i + 1,
                name=c.get("name", c.get("id", "")),
                kws=", ".join(str(k) for k in c.get("keywords", [])),
                fc=len(c.get("files", [])),
            )
            for i, c in enumerate(cluster)
        )
        prompt = _DEDUP_PROMPT.format(card_lines=card_lines)
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
        groups = _parse_groupings(raw, len(cluster))
        if groups is None:
            return cluster
        return [_merge_card_group([cluster[i] for i in g]) for g in groups]
    except Exception:  # noqa: BLE001
        _log.exception("context index: LLM dedup failed for cluster, keeping it unchanged")
        return cluster


def _dedup_fallback(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """No-LLM fallback: greedily merge cards with file-overlap Jaccard >= _DEDUP_FALLBACK_JACCARD."""
    merged: List[Dict[str, Any]] = []
    for card in cards:
        files_a = set(card.get("files", []))
        matched = False
        for rep in merged:
            if _jaccard(files_a, set(rep.get("files", []))) >= _DEDUP_FALLBACK_JACCARD:
                rep["files"] = list(dict.fromkeys(rep.get("files", []) + card.get("files", [])))
                rep["keywords"] = list(
                    dict.fromkeys(rep.get("keywords", []) + card.get("keywords", []))
                )
                matched = True
                break
        if not matched:
            merged.append({**card})
    return merged


def _dedup_topic_cards(
    raw_cards: List[Dict[str, Any]],
    provider,
    model=None,
    existing_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Stage 3: dedup topic cards by keyword-clustering + an LLM merge decision.

    Step 1 — cluster cards (new + existing) by shared keywords (>= 2 shared keywords, transitive
             via union-find). Singletons pass through unchanged.
    Step 2 — for each cluster of 2+ NEW cards (no existing card in it), one LLM call decides the
             merge groupings; on parse failure the cluster is kept unchanged.
    Step 3 — for any cluster that contains an EXISTING card, merge the new cards INTO the existing
             one (keeping the existing card's id) rather than emitting a new card with a new id.

    When ``provider is None`` falls back to a keyword-union merge (Jaccard >= 30%).

    Returns the deduplicated list of NEW/updated cards to write. Existing-only clusters (no new
    card) contribute nothing new to write here unless a new card merged into them.
    """
    if not raw_cards:
        return []

    existing_cards = existing_cards or []

    # Step 0 -- the FILE SET, before any keyword gate and before any model call. Two cards pinning
    # the same paths are one card, and no judgement is needed to see that. Runs against the
    # existing cards too, so a restatement of a card already in the store folds into it.
    raw_cards = collapse_cards_by_file_set(raw_cards, existing_cards)
    if not raw_cards:
        return []

    if provider is None:
        # No LLM: keyword-union fallback over the new cards only. (Existing-card dedup needs the
        # LLM's judgment; without it we keep new cards distinct and let id-collision upsert handle
        # exact-id matches downstream.)
        return _dedup_fallback(raw_cards)

    # Combine new + existing for clustering so a new card that duplicates an existing one lands in
    # the same cluster. Tag each with its origin so we can route the result correctly.
    combined: List[Dict[str, Any]] = list(raw_cards) + list(existing_cards)
    is_existing = [False] * len(raw_cards) + [True] * len(existing_cards)

    clusters = _keyword_clusters(combined)
    out: List[Dict[str, Any]] = []
    for cluster_idx in clusters:
        has_existing = any(is_existing[i] for i in cluster_idx)
        new_members = [combined[i] for i in cluster_idx if not is_existing[i]]

        if not new_members:
            # Cluster of only existing cards — nothing new to write.
            continue

        if has_existing:
            # Merge the new cards INTO the first existing card in the cluster, preserving its id.
            existing_member = next(combined[i] for i in cluster_idx if is_existing[i])
            merged = _merge_card_group([existing_member] + new_members)
            out.append(merged)
            continue

        if len(new_members) == 1:
            out.append({**new_members[0]})
            continue

        # Pure-new cluster of 2+: let the LLM decide the merge groupings.
        out.extend(_dedup_cluster_llm(new_members, provider, model))

    # Guard against an existing id colliding with a fresh id we keep: dedup by id, first wins.
    seen_ids: Set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for card in out:
        cid = card.get("id")
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        deduped.append(card)
    return deduped
