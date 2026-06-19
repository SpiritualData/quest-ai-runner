"""Shared card-content rendering — rank a card's typed content items by recency + relevance and
resolve each through the wired ``ReferenceResolver`` registry into fresh context lines.

A context card (see ``file_context_store``) carries an optional ``content`` list of TYPED items,
each either a REFERENCE (``file`` / ``collection`` / ``conversation`` / ``query``) resolved FRESH on
every use, or an LLM ``note``. This module owns the ONE shared routine that turns those items into
rendered context, so a selected card's references resolve to live content REGARDLESS of which
retrieval arm selected it:

  * the keyword/IDF arm (``FileContextStore``), and
  * the semantic arm (``VectorContextAssembler``).

Before this was shared, resolution lived only in ``FileContextStore``; a card selected by the vector
arm rendered its description but never resolved its references, so the live collection/conversation
data the card points at was silently dropped. Centralizing the logic here fixes that and guarantees
the two arms can never drift.

Generic by construction (hard rule #2): nothing here knows about any org, collection, or filesystem
layout. Resolution goes entirely through the injected ``{type: ReferenceResolver}`` registry; a type
with no resolver degrades to a graceful unresolved-pointer line (never an error).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Set

from .reference_resolver import _render_unresolved

# ---------------------------------------------------------------------------
# Recency-bound limits for resolving a card's ``content`` during assemble().
# ---------------------------------------------------------------------------
# Max number of content REFERENCES resolved/rendered per card.
MAX_CARD_REFS = 8
# Soft char budget for ALL resolved content of a single card (across its items).
MAX_CARD_REF_CHARS = 4000
# Per-item soft char cap handed to a resolver (so one huge item can't eat the whole budget).
MAX_CARD_REF_ITEM_CHARS = 2000
# Hard cap on how many content items a card retains on disk (oldest trimmed on write).
MAX_CARD_CONTENT_ITEMS = 200

# ---------------------------------------------------------------------------
# Short stopwords dropped from keyword tokenization (pure ASCII, lowercase).
# ---------------------------------------------------------------------------
_STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "its", "me", "my",
    "not", "of", "on", "or", "that", "the", "this", "to", "was", "we",
    "what", "when", "where", "which", "who", "will", "with", "you",
}
_MIN_TOKEN_LEN = 3  # tokens shorter than this are always dropped


def tokenize(text: str) -> Set[str]:
    """Lowercase-tokenize ``text`` to a keyword set, dropping short tokens and stopwords.

    Splits camelCase/PascalCase before extracting tokens so that ``StatusTick``
    indexes under both ``status`` and ``tick`` (and queries for either word match).
    """
    # camelCase split: 'statusTick' -> 'status Tick'
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # acronym boundary: 'parseHTML' -> 'parse HTML', 'HTMLParser' -> 'HTML Parser'
    text = re.sub(r'([A-Z]{2,})([A-Z][a-z])', r'\1 \2', text)
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def content_item_text(item: Dict[str, Any]) -> str:
    """The free text a content item carries for relevance scoring (its ``why`` + any note text).

    A reference contributes its ``why`` (the short reason it is on the card); a note contributes
    both its ``why`` and its locator ``text``. This is what the recency+relevance ranker tokenizes
    so a pure note/collection card is still searchable. Never raises.
    """
    try:
        parts: List[str] = []
        why = item.get("why")
        if isinstance(why, str) and why:
            parts.append(why)
        if item.get("type") == "note":
            text = (item.get("locator") or {}).get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return " ".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def normalize_content(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a card's ``content`` field into a clean list of well-formed item dicts. Never raises.

    Drops anything that is not a dict, defaults a missing ``type`` to ``note``, ensures ``locator``
    is a dict, coerces ``ts`` to a float (0.0 when absent/bad), and synthesizes a stable ``id`` when
    one is missing. Order is preserved. A card with no ``content`` key yields ``[]`` (backward
    compatible: such a card behaves exactly as a file-only card).
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "note").strip() or "note"
        locator = item.get("locator")
        if not isinstance(locator, dict):
            locator = {}
        try:
            ts = float(item.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        why = item.get("why")
        why = why if isinstance(why, str) else ""
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            # Stable-ish id from type + a digest of the locator + index, so updates can target it.
            digest = hashlib.sha256(
                (itype + json.dumps(locator, sort_keys=True, default=str)).encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()[:8]
            item_id = f"{itype}-{digest}-{idx}"
        out.append({"id": item_id, "type": itype, "locator": locator, "ts": ts, "why": why})
    return out


def rank_content_by_recency_relevance(
    content: List[Dict[str, Any]], task_kws: Set[str], *, limit: int
) -> List[Dict[str, Any]]:
    """Rank content items by recency (``ts``) + relevance (term overlap), return the top ``limit``.

    A card's content can grow huge over time, so resolution must be bounded. We score each item by
    a blend: a recency component (newer ``ts`` ranks higher, normalized within the card) plus a
    relevance component (overlap of the item's text terms with the task keywords). Relevance is
    weighted more so a clearly on-topic-but-older item can still beat a fresh-but-irrelevant one,
    while recency breaks ties and gently deprioritizes the stale. Never raises.
    """
    try:
        if not content:
            return []
        ts_values = [it.get("ts", 0.0) for it in content]
        ts_min, ts_max = min(ts_values), max(ts_values)
        ts_span = (ts_max - ts_min) or 1.0

        scored: List[tuple] = []
        for it in content:
            # Use the card's own tokenizer (camelCase + whitespace split, the same one assemble()
            # tokenizes the task with) so relevance overlap is computed on a consistent vocabulary.
            terms = tokenize(content_item_text(it)) if task_kws else set()
            overlap = len(terms & task_kws) if task_kws else 0
            recency = (it.get("ts", 0.0) - ts_min) / ts_span  # 0.0 (oldest) .. 1.0 (newest)
            # Relevance dominates; recency is a smaller additive nudge / tie-breaker.
            score = (2.0 * overlap) + recency
            scored.append((score, it.get("ts", 0.0), it))
        # Sort by score desc, then ts desc (newest first on ties).
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [it for _, _, it in scored[: max(0, limit)]]
    except Exception:  # noqa: BLE001
        return content[: max(0, limit)]


def render_card_content(
    card: Dict[str, Any],
    resolvers: Dict[str, Any],
    *,
    task_kws: Set[str],
    max_refs: int = MAX_CARD_REFS,
    max_ref_chars: int = MAX_CARD_REF_CHARS,
    max_ref_item_chars: int = MAX_CARD_REF_ITEM_CHARS,
) -> List[str]:
    """Render a card's source-agnostic ``content`` items into context lines. Never raises.

    THE shared resolution routine for BOTH retrieval arms. Recency-bounded: ranks the card's content
    by recency (``ts``) + relevance to ``task_kws``, then resolves only the top ``max_refs`` items
    within the ``max_ref_chars`` char budget (skipping/trimming the rest). Each item resolves FRESH
    through the ``resolvers`` registry entry for its ``type``; a type with no resolver renders a
    graceful unresolved-pointer line. Returns a list of rendered lines (possibly empty), ready to
    fold into a card's view block.

    ``resolvers`` is a ``{type: ReferenceResolver}`` registry (see ``reference_resolver.py``).
    ``task_kws`` is the tokenized task text (use ``tokenize()``), used to rank which items resolve.
    """
    lines: List[str] = []
    try:
        content = normalize_content(card.get("content"))
        if not content:
            return lines
        ranked = rank_content_by_recency_relevance(content, task_kws, limit=max_refs)
        budget = max_ref_chars
        for item in ranked:
            if budget <= 0:
                break
            itype = item.get("type", "note")
            locator = item.get("locator", {})
            why = item.get("why", "")
            resolver = (resolvers or {}).get(itype)
            item_cap = min(max_ref_chars, budget, max_ref_item_chars)
            if resolver is not None:
                rendered = ""
                try:
                    rendered = resolver.resolve(locator, max_chars=item_cap) or ""
                except Exception:  # noqa: BLE001 — a resolver must never break assembly
                    rendered = ""
                if not rendered:
                    # Resolved to nothing (e.g. a deleted file): surface it, don't drop silently.
                    rendered = _render_unresolved(itype, locator)
            else:
                # No resolver wired for this type: graceful unresolved-pointer line.
                rendered = _render_unresolved(itype, locator)
            if len(rendered) > budget:
                rendered = rendered[: budget - 1].rstrip() + "…"
            budget -= len(rendered)
            header = f"  - ({itype})" + (f" {why}" if why else "")
            lines.append(header)
            for rl in rendered.splitlines() or [rendered]:
                lines.append(f"      {rl}")
    except Exception:  # noqa: BLE001
        return lines
    return lines
