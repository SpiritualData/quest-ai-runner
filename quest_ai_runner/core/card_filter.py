"""Card filter — LLM-based relevance filtering for context cards."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from .adapters import ModelProvider

_log = logging.getLogger("quest-ai-runner.card-filter")


# ---------------------------------------------------------------------------
# Selection memo — skip a repeated LLM SELECTION when nothing changed.
# ---------------------------------------------------------------------------
# A tiny in-process, bounded PER-PROVIDER LRU shared by the two LLM SELECTION entry points in this
# module (``filter_cards_by_relevance`` and ``consolidate_context``). A repeat ask whose candidate
# cards (ids + their content) and topic keywords are UNCHANGED resolves to the same cache key and
# skips the LLM call entirely, returning a COPY of the prior verdict. Staleness is impossible by
# construction: the key hashes every input that can change the verdict (each candidate's id + a
# content fingerprint, the topic keywords, the resolved model id, and any usage hint), so ANY change
# misses the cache and recomputes. Provider identity is handled by STRUCTURE, not by key: each
# provider instance owns its own LRU inside a ``WeakKeyDictionary``, so a different provider can
# never be served another provider's verdict, and a provider's entries die WITH the provider (a
# fresh provider built with identical inputs, e.g. per test, always starts cold -- this is what
# makes test isolation automatic; ``str(id(provider))`` in the key was tried first and is unsound
# because id() values are reused after garbage collection). A provider that cannot be weak-
# referenced or hashed is simply never memoized (correct, just uncached). The memo only engages
# when a real provider is wired -- the no-provider fallbacks stay byte-for-byte identical and are
# never cached.
_SELECTION_MEMO_MAX = 64
_selection_memos: "weakref.WeakKeyDictionary[Any, OrderedDict]" = weakref.WeakKeyDictionary()
_selection_memo_lock = threading.Lock()


def _topic_keywords(task: str) -> str:
    """A stable, order-independent topic-keyword signature of the task text.

    Lowercased alphanumeric tokens, deduped and sorted, so paraphrases that differ only in word
    order / casing / punctuation still hit the same memo entry (the memo is keyword-scoped, not
    exact-string-scoped, matching how retrieval already treats the task).
    """
    return " ".join(sorted(set(re.findall(r"[a-z0-9]+", (task or "").lower()))))


def _selection_key(
    tag: str,
    task: str,
    signatures: List[str],
    *,
    model: Optional[str],
    extra: str = "",
) -> str:
    """Build the memo key. ``signatures`` are sorted so candidate ORDER never changes the key.
    The provider is NOT part of the key: it is the ``WeakKeyDictionary`` key of the per-provider
    LRU the entry lives in (see the module comment above)."""
    h = hashlib.sha256()
    for part in (tag, _topic_keywords(task), model or "", extra):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    for sig in sorted(signatures):
        h.update(sig.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _memo_get(provider: Any, key: str) -> Any:
    with _selection_memo_lock:
        try:
            memo = _selection_memos.get(provider)
        except TypeError:  # unhashable provider: never memoized
            return None
        if memo is not None and key in memo:
            memo.move_to_end(key)
            return memo[key]
    return None


def _memo_put(provider: Any, key: str, value: Any) -> None:
    with _selection_memo_lock:
        try:
            memo = _selection_memos.get(provider)
            if memo is None:
                memo = OrderedDict()
                _selection_memos[provider] = memo
        except TypeError:  # unhashable or non-weak-referenceable provider: skip caching
            return
        memo[key] = value
        memo.move_to_end(key)
        while len(memo) > _SELECTION_MEMO_MAX:
            memo.popitem(last=False)


def clear_selection_memo() -> None:
    """Drop every memoized selection verdict (also safe to call in production). Tests do NOT need
    to call this: isolation is automatic because each provider's entries die with the provider."""
    with _selection_memo_lock:
        _selection_memos.clear()


def _copy_consolidation(verdict: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deep-ish copy of a consolidation verdict so a cached value can never be mutated by a caller."""
    return [
        {
            "card_id": c.get("card_id", ""),
            "priority_rank": c.get("priority_rank"),
            "items": [dict(it) for it in (c.get("items") or [])],
        }
        for c in verdict
    ]


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
    recent_item_usage = recent_item_usage or {}

    # Selection memo: identical merged card set + topic keywords + usage hint -> skip the LLM call.
    signatures: List[str] = []
    for card in cards:
        items_sig = ",".join(
            f"{it.get('id', '')}:{(it.get('preview', '') or '')[:80]}"
            for it in (card.get("items") or []) if isinstance(it, dict)
        )
        signatures.append(
            f"{card.get('id', '')}|{card.get('title', '')}|{(card.get('preview', '') or '')[:80]}|{items_sig}"
        )
    usage_sig = ";".join(
        f"{cid}={','.join(ids or [])}" for cid, ids in sorted(recent_item_usage.items())
    )
    memo_key = _selection_key(
        "consolidate", task, signatures, model=model, extra=usage_sig,
    )
    cached = _memo_get(model_provider, memo_key)
    if cached is not None:
        return _copy_consolidation(cached)

    try:
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
        ordered = stable_card_order(result)
        # Cache only a real LLM verdict: a keep-all FALLBACK is never memoized, so a transient
        # parse failure is retried next time rather than pinned.
        _memo_put(model_provider, memo_key, _copy_consolidation(ordered))
        return _copy_consolidation(ordered)
    except Exception as e:  # noqa: BLE001
        _log.debug("consolidate_context failed, keeping all cards/items: %s", e)
        return _consolidate_keep_all(cards)


# ---------------------------------------------------------------------------
# Batched within-card file ranking: ONE LLM call ranks files for ALL selected cards.
# ---------------------------------------------------------------------------
# Replaces the old one-LLM-call-per-card loop (up to N parallel calls, one per relevant card). A
# single prompt lists every selected card with its file list; the model returns per-card file scores
# in one JSON object. Bounds keep the prompt from blowing up on a huge selection: cap the cards shown
# and the files shown per card (the returned ranking still yields the top 5 files per card, the same
# contract the per-card loop had).
_RANK_MAX_CARDS = 24        # cards shown in one batched ranking prompt
_RANK_MAX_FILES = 40        # files shown per card in the prompt (then top 5 are returned)

_RANK_FILES_PROMPT = """You are a code relevance expert. Given a task and several context cards, \
rank the files WITHIN EACH card by how relevant they are to the task.

TASK: {task}

CARDS (each lists its own files):
{cards_block}

For every card, score each of its files 0-1 where:
- 1 = essential for this task
- 0.5 = might be useful
- 0 = not relevant to this task

Respond with ONLY valid JSON (no markdown, no extra text). Score files under the card they belong \
to, using the card id shown in brackets:
{{
  "cards": [
    {{"card_id": "<card id>", "files": [{{"path": "path/to/file.py", "score": 0.95}}]}}
  ]
}}"""


def _rank_files_batched(
    task: str,
    cards_with_files: List[tuple],
    *,
    model_provider: ModelProvider,
    model: Optional[str],
) -> Dict[str, List[str]]:
    """ONE LLM call ranking files within ALL cards that have files. Returns ``{card_id: [top-5]}``.

    ``cards_with_files`` is a list of ``(card_dict, score)``. On ANY failure (call error, unparseable
    JSON, wrong shape) returns ``{}`` so the caller falls back to each card's original file order --
    never raises, never retries in a loop. ``model`` is the caller-resolved tier (e.g. "balanced");
    it is passed through so this ranking uses the SAME cheap tier as the card-level pass instead of
    silently defaulting to the provider's most expensive model.
    """
    if not cards_with_files:
        return {}
    blocks: List[str] = []
    for card, _ in cards_with_files[:_RANK_MAX_CARDS]:
        cid = card.get("id", "")
        title = card.get("title", "Untitled")
        files = card.get("files", [])[:_RANK_MAX_FILES]
        file_lines = "\n".join(f"  - {f}" for f in files)
        blocks.append(f"[{cid}] {title}\n{file_lines}")
    prompt = _RANK_FILES_PROMPT.format(task=task, cards_block="\n\n".join(blocks))
    try:
        raw = model_provider.answer([{"role": "user", "content": prompt}], model=model)
        parsed = json.loads(_extract_json(raw or "") or "{}")
    except Exception as e:  # noqa: BLE001
        _log.debug("batched file ranking failed, using original file order: %s", e)
        return {}
    cards_out = parsed.get("cards") if isinstance(parsed, dict) else parsed
    if not isinstance(cards_out, list):
        return {}
    files_by_id = {c.get("id", ""): c.get("files", []) for c, _ in cards_with_files}
    ranked: Dict[str, List[str]] = {}
    for entry in cards_out:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("card_id", "")
        if cid not in files_by_id:
            continue
        scored = entry.get("files")
        if not isinstance(scored, list):
            continue
        score_map: Dict[str, float] = {}
        for f in scored:
            if isinstance(f, dict):
                score_map[f.get("path", "")] = f.get("score", 0.5)
        card_files = files_by_id[cid]
        ranked[cid] = sorted(
            card_files, key=lambda f: score_map.get(f, 0), reverse=True
        )[:5]
    return ranked


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
    1. Card-level: score each card 0-1 by relevance to the task (ONE LLM call)
    2. File-level: within the selected cards, rank files by relevance (ONE batched LLM call over
       ALL selected cards -- not one call per card)

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

    # Selection memo: identical candidate set (ids + file lists) + topic keywords -> skip both LLM
    # calls and reuse the prior verdict (returned as COPIES so the cache is never mutated).
    signatures = [
        f"{c.get('id', '')}|{c.get('title', '')}|{','.join(c.get('files', []) or [])}"
        for c in candidate_cards
    ]
    memo_key = _selection_key("filter", task, signatures, model=model)
    cached = _memo_get(model_provider, memo_key)
    if cached is not None:
        return [replace(m, files=list(m.files)) for m in cached]

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

    stage1_ok = True
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
        stage1_ok = False

    # --- Stage 2: File-level relevance ranking (within selected cards) ---
    # Filter to only relevant cards, then rank their files in ONE batched LLM call (not one call
    # per card). The batched ranking uses the caller-resolved ``model`` tier (e.g. "balanced"), not
    # the provider's expensive default.
    relevant_cards = []
    for card in candidate_cards:
        card_id = card.get("id", "")
        score = card_scores.get(card_id, 0)
        if score >= 0.5:
            relevant_cards.append((card, score))

    # Only cards that actually have files need ranking; a file-less card keeps an empty file list.
    cards_with_files = [(c, s) for (c, s) in relevant_cards if c.get("files")]
    ranked_by_card = _rank_files_batched(
        task, cards_with_files, model_provider=model_provider, model=model,
    )

    results = []
    for card, score in relevant_cards:
        card_id = card.get("id", "")
        card_files = card.get("files", [])
        # Ranked top-5 when the batched call named this card; otherwise the pre-existing non-LLM
        # ordering (original order, top 5) -- the same fallback the per-card loop used.
        files = ranked_by_card.get(card_id, card_files[:5])
        results.append(CardMetadata(
            id=card_id,
            title=card.get("title", ""),
            file_count=len(card_files),
            files=files,
            relevance_score=score,
            adapter=card.get("adapter", ""),
        ))

    # Presentation order is STABLE by card id, not by relevance score: provider prompt caches
    # are PREFIX caches, so re-sorting the same selected cards by a score that drifts call to
    # call defeats caching for the whole layer (measured: caching with a reshuffled card set
    # costs MORE than no caching at all). relevance_score above already carries the LLM's
    # usefulness judgment for any caller that needs "the most relevant card".
    results.sort(key=lambda r: r.id)
    # Cache only a real stage-1 verdict: the neutral-score FALLBACK (stage-1 call/parse failure)
    # is never memoized, so a transient failure is retried next time rather than pinned (same
    # rule as ``consolidate_context``'s keep-all fallback).
    if stage1_ok:
        _memo_put(model_provider, memo_key, [replace(m, files=list(m.files)) for m in results])
    return results
