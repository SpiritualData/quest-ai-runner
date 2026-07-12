"""Card filter — LLM-based relevance filtering for context cards."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .adapters import ModelProvider

_log = logging.getLogger("quest-ai-runner.card-filter")


def _extract_json(text: str) -> str:
    """Strip markdown fences and return the first JSON object or array substring."""
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    # Find first { or [
    for start_ch, end_ch in [('{', '}'), ('[', ']')]:
        idx = text.find(start_ch)
        if idx < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(idx, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    return text[idx:i + 1]
    return ""


# ---------------------------------------------------------------------------
# Consolidating holistic filter: ONE LLM call over the MERGED card set.
# ---------------------------------------------------------------------------
# After both retrieval arms each filter their own cards, the hybrid runs this single consolidating
# pass over the union: it drops tangential/redundant cards ACROSS arms, reranks them, and prunes
# which content ITEMS inside each kept card survive. The content is never rewritten here, the LLM
# only selects card ids + item ids (and a per-item delivery tag), so everything stays VERBATIM.

# Bound the consolidation prompt so a huge card set can never blow up the call.
_CONSOLIDATE_MAX_CARDS = 40        # cards shown to the consolidator
_CONSOLIDATE_MAX_ITEMS = 20        # items shown per card
_CONSOLIDATE_MAX_PREVIEW = 200     # chars of preview shown per item

_CONSOLIDATE_PROMPT = """You are a context relevance editor. Given a task and a set of context \
cards, select ONLY the cards and the specific content items that genuinely help with the task, in \
priority order.

TASK:
{task}

CARDS (each card lists its content items with a short preview):
{cards_block}

Rules:
- Keep only cards relevant to the task. Drop tangential or redundant cards entirely.
- Order the kept cards by usefulness, most useful first.
- Within each kept card, keep only the items that add something. Drop redundant or off-topic items.
- Some cards list NO items (they are file/reference cards whose value is their summary and file \
listings). Judge them at the CARD level: to keep one, return it with an empty "items" list; to drop \
it, omit it.
- A card may note which item ids were recently useful for a SIMILAR past input. Treat this as a \
hint, not a rule: when one of those items still genuinely helps THIS task, keep it and list it \
first among that card's kept items; when it does not help this task, drop it like any other item.
- For each kept item, choose how to deliver it:
  "paste" (the default): the item's content is included directly.
  "pointer": only for a file the worker can open by itself later, when pasting its full text now is \
not needed.
  When unsure, use "paste".

Respond with ONLY valid JSON (no markdown, no prose). The "cards" list is in priority order:
{{
  "cards": [
    {{"card_id": "<card id>", "items": [{{"item_id": "<item id>", "deliver": "paste"}}]}}
  ]
}}
Return only the cards and items you are keeping."""


def stable_card_order(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stamp a ``priority_rank`` (0 = most useful) from the entries' current order, then return
    them sorted by ``card_id`` (lexicographic) instead of by usefulness.

    Selection stays intelligent (whatever decided ``entries``' input order, keep it); only the
    RETURNED order changes. Provider prompt caches are PREFIX caches: a card set that renders in
    a different order every call defeats caching for the whole layer even when the same cards are
    selected turn after turn (measured: caching a call whose card order was reshuffled costs MORE
    than no caching at all, because every call becomes a fresh cache write). Sorting by card id
    makes the rendered block byte-identical across calls whenever the selection is unchanged. A
    caller that wants "the most useful card" reads ``priority_rank`` on each entry, never
    position 0 in the returned list.
    """
    for rank, entry in enumerate(entries):
        entry["priority_rank"] = rank
    return sorted(entries, key=lambda e: e.get("card_id", ""))


def _consolidate_keep_all(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Graceful fallback verdict: keep EVERY card and EVERY item, deliver=paste.

    This is the "never worse" output: the consolidated selection equals exactly the mechanical
    merge (every card/item survives), so when no provider is wired (or anything fails) the kept
    set is identical to today. The RETURNED order is still stabilized by card id (see
    ``stable_card_order``) so this fallback path is just as cache-friendly as a real verdict.
    """
    out: List[Dict[str, Any]] = []
    for card in cards:
        items = card.get("items") or []
        out.append({
            "card_id": card.get("id", ""),
            "items": [
                {"item_id": it.get("id", ""), "deliver": "paste"}
                for it in items if isinstance(it, dict)
            ],
        })
    return stable_card_order(out)


def _validate_consolidation(
    parsed: Any, cards: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """Validate an LLM consolidation verdict against the known cards/items. None on hard failure.

    ``parsed`` is the decoded LLM JSON: either the ``{"cards": [...]}`` wrapper (the shape the
    prompt asks for, which ``_extract_json`` decodes cleanly) or a bare list. Returns the cleaned,
    ordered keep-list (only known card ids, only known item ids within their card, each appearing
    once, ``deliver`` normalized to "paste"|"pointer"). Cards/items the LLM did not name are DROPPED.
    Returns None when the shape is unusable or no kept card survives, so the caller can fall back to
    keep-all (the never-worse guarantee).
    """
    if isinstance(parsed, dict):
        parsed = parsed.get("cards")
    if not isinstance(parsed, list):
        return None
    # Map each known card id to its set of known item ids (preserves which items are real).
    known: Dict[str, set] = {}
    for card in cards:
        cid = card.get("id", "")
        if not cid:
            continue
        known[cid] = {
            it.get("id", "") for it in (card.get("items") or []) if isinstance(it, dict)
        }
    out: List[Dict[str, Any]] = []
    seen_cards: set = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("card_id", "")
        if cid not in known or cid in seen_cards:
            continue
        kept_items: List[Dict[str, Any]] = []
        seen_items: set = set()
        for it in (entry.get("items") or []):
            if not isinstance(it, dict):
                continue
            iid = it.get("item_id", "")
            if iid not in known[cid] or iid in seen_items:
                continue
            deliver = "pointer" if it.get("deliver") == "pointer" else "paste"
            kept_items.append({"item_id": iid, "deliver": deliver})
            seen_items.add(iid)
        if not kept_items and known[cid]:
            # An ITEM-BEARING card whose every item was pruned is a fully-dropped card.
            continue
        # A file-only card (no known items) named by the LLM is kept WHOLE with an empty item list:
        # it has nothing to prune, but its rendered section (summary + file listings) still matters.
        seen_cards.add(cid)
        out.append({"card_id": cid, "items": kept_items})
    if not out:
        return None
    return out


def consolidate_context(
    task: str,
    cards: List[Dict[str, Any]],
    *,
    model_provider: Optional[ModelProvider] = None,
    model: Optional[str] = None,
    recent_item_usage: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """ONE holistic LLM pass over the merged card set: drop, rerank, and prune content items.

    ``cards`` is the merged card set, each ``{"id", "title", "items": [{id, type, why, preview}]}``
    (the previews come from ``render_card_content_blocks``). Returns the CONSOLIDATOR OUTPUT,
    ``[{"card_id", "priority_rank", "items": [{"item_id", "deliver": "paste"|"pointer"}]}]``, only
    the surviving item ids per card.

    SELECTION is still fully LLM-driven: the model decides which cards/items survive, and its
    usefulness judgment is captured verbatim as ``priority_rank`` on each entry (0 = most useful,
    1 = next, ...). The RETURNED list order is deliberately NOT that priority order: it is sorted
    by ``card_id`` (lexicographic) instead, so the rendered prompt stays byte-identical across
    calls whenever the selection doesn't change. Provider prompt caches are PREFIX caches, so a
    card set that renders in a different order every call defeats caching for the whole layer
    (measured: caching a call whose card order was reshuffled costs MORE than no caching, because
    every call becomes a fresh cache write instead of a cache read). A caller that wants "the most
    useful card" reads ``priority_rank``, never position 0.

    ``recent_item_usage`` (optional) is the ``{card_id: [item_id, ...]}`` hint built from the warm
    recent-context store's item-usage memory (see ``core.recent_context.build_item_usage_hint``):
    item ids a past turn with a SIMILAR input already found useful for that card, ranked most
    relevant first. When a hint names ids present on a candidate card, the prompt tells the LLM to
    prefer keeping (and ordering first) those items -- a HINT the consolidator may still override
    when they no longer serve THIS task, never a hard rule.

    The LLM selects ids ONLY, never rewrites content, so everything stays VERBATIM. Graceful: when
    ``model_provider`` is None, or the call/parse/validation fails, returns keep-all with
    deliver=paste in the original order (identical to the mechanical merge). Never raises.
    """
    if not cards:
        return []
    if model_provider is None:
        return _consolidate_keep_all(cards)
    try:
        recent_item_usage = recent_item_usage or {}
        card_lines: List[str] = []
        for card in cards[:_CONSOLIDATE_MAX_CARDS]:
            cid = card.get("id", "")
            title = card.get("title", "") or "(untitled)"
            # A card-level preview (used mainly for item-less file/reference cards) so the LLM can
            # judge their relevance without any item lines below.
            preview = (card.get("preview", "") or "")[:_CONSOLIDATE_MAX_PREVIEW]
            head = f"[{cid}] {title}"
            if preview and preview != title:
                head += f" :: {preview}"
            card_lines.append(head)
            # Recent-turn item-usage hint for THIS card (see core.recent_context): only the ids
            # that are actually candidates on this card are worth naming.
            known_item_ids = {
                it.get("id", "") for it in (card.get("items") or []) if isinstance(it, dict)
            }
            recent_ids = [iid for iid in recent_item_usage.get(cid, []) if iid in known_item_ids]
            if recent_ids:
                card_lines.append(
                    "  (recently useful for a similar input: " + ", ".join(recent_ids) + ")")
            for it in (card.get("items") or [])[:_CONSOLIDATE_MAX_ITEMS]:
                if not isinstance(it, dict):
                    continue
                iid = it.get("id", "")
                itype = it.get("type", "note")
                why = (it.get("why", "") or "").strip()
                preview = (it.get("preview", "") or "")[:_CONSOLIDATE_MAX_PREVIEW]
                meta = " | ".join(p for p in (why, preview) if p)
                card_lines.append(f"  - ({iid}) {itype}" + (f": {meta}" if meta else ""))
        prompt = _CONSOLIDATE_PROMPT.format(task=task, cards_block="\n".join(card_lines))
        raw = model_provider.answer([{"role": "user", "content": prompt}], model=model)
        parsed = json.loads(_extract_json(raw or "") or "[]")
        result = _validate_consolidation(parsed, cards)
        if result is None:
            return _consolidate_keep_all(cards)
        return stable_card_order(result)
    except Exception as e:  # noqa: BLE001
        _log.debug("consolidate_context failed, keeping all cards/items: %s", e)
        return _consolidate_keep_all(cards)


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
    model: Optional[str] = None,
) -> List[CardMetadata]:
    """Use LLM to filter context cards by relevance to the task.

    Two-stage filtering:
    1. Card-level: score each card 0-1 by relevance to the task
    2. File-level: within each selected card, rank files by relevance

    Returns only cards with relevance > 0.5. SELECTION is still LLM-driven (the score decides
    which cards survive), but the RETURNED order is stable: sorted by card id (lexicographic),
    not by score. Provider prompt caches are PREFIX caches, so a card set that renders in a
    different order every call (as relevance scores drift turn to turn) defeats caching for the
    whole layer -- the score survives as ``relevance_score`` on each ``CardMetadata`` so a caller
    that wants "the most useful card" reads that field, never position 0.
    Files within each card are still ordered by relevance DESC (a within-card ranking, not the
    top-level card order the prefix cache depends on).

    Args:
        task: The user's task text
        candidate_cards: List of {id, title, files, adapter} from retrieval
        model_provider: Optional ModelProvider for LLM scoring
                       (if None, returns all cards with files in original order)

    Returns:
        Filtered CardMetadata list, ordered by card id (stable); relevance_score carries rank.
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
            model=model,
        )
        card_scores_raw = json.loads(_extract_json(card_scores_json or "") or "{}")
        card_scores = {c["id"]: c["score"] for c in (card_scores_raw.get("cards") or [])}
    except Exception as e:
        _log.debug("card-level scoring failed, falling back: %s", e)
        # Fallback: neutral scores for all
        card_scores = {c.get("id", ""): 0.7 for c in candidate_cards}

    # --- Stage 2: File-level relevance ranking (within selected cards, PARALLEL) ---
    # Filter to only relevant cards, then score files in parallel
    relevant_cards = []
    for card in candidate_cards:
        card_id = card.get("id", "")
        score = card_scores.get(card_id, 0)
        if score >= 0.5:
            relevant_cards.append((card, score))

    # Define work unit: one card's file ranking
    def _rank_files_for_card(card_and_score: tuple) -> CardMetadata:
        card, score = card_and_score
        card_id = card.get("id", "")
        card_files = card.get("files", [])

        if not card_files:
            return CardMetadata(
                id=card_id,
                title=card.get("title", ""),
                file_count=0,
                files=[],
                relevance_score=score,
                adapter=card.get("adapter", ""),
            )

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
                model=None,
            )
            file_scores_raw = json.loads(_extract_json(file_scores_json or "") or "{}")
            # Build path -> score map from response
            file_scores_map = {}
            for f in (file_scores_raw.get("files") or []):
                path = f.get("path", "")
                score_val = f.get("score", 0.5)
                file_scores_map[path] = score_val
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

        return CardMetadata(
            id=card_id,
            title=card.get("title", ""),
            file_count=len(card_files),
            files=ranked_files,
            relevance_score=score,
            adapter=card.get("adapter", ""),
        )

    # Run file ranking IN PARALLEL for all relevant cards
    results = []
    if relevant_cards:
        try:
            # Use ThreadPoolExecutor to run file-ranking prompts concurrently
            # Cap at min(8, len(relevant_cards)) workers to avoid overwhelming the LLM
            max_workers = min(8, len(relevant_cards))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                ranked_metadata = list(executor.map(_rank_files_for_card, relevant_cards))
                results = ranked_metadata
        except Exception as e:
            _log.debug("parallel file ranking failed, falling back to sequential: %s", e)
            # Fallback: sequential ranking
            results = [_rank_files_for_card(cs) for cs in relevant_cards]

    # Presentation order is STABLE by card id, not by relevance score: provider prompt caches
    # are PREFIX caches, so re-sorting the same selected cards by a score that drifts call to
    # call defeats caching for the whole layer (measured: caching with a reshuffled card set
    # costs MORE than no caching at all). relevance_score above already carries the LLM's
    # usefulness judgment for any caller that needs "the most relevant card".
    results.sort(key=lambda r: r.id)
    return results
