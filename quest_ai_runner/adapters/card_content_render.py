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


def content_identity_key(item: Dict[str, Any]) -> str:
    """A stable identity for a content item, used to DEDUPE references that point at the SAME thing.

    Two items are the same reference when they point at the same external target: a collection with
    the same id (else the same name), a file at the same path, otherwise a note identified by its
    text. Anything else falls back to the full ``(type, locator)`` shape. The key is lowercased and
    stripped so trivial casing/whitespace differences still collapse. Returns a hashable string.
    Never raises.
    """
    try:
        itype = str(item.get("type") or "note").strip() or "note"
        loc = item.get("locator") if isinstance(item.get("locator"), dict) else {}
        salient: Any = None
        if itype == "collection":
            salient = loc.get("id") or loc.get("name")
        elif itype == "file":
            salient = loc.get("path")
        elif itype == "note":
            salient = (loc.get("text") or "").strip()
        if salient:
            return f"{itype}:{str(salient).strip().lower()}"
        return itype + ":" + json.dumps(loc, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(item.get("type") or "note")


def dedupe_content(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse content items that point at the SAME reference into ONE merged item per identity.

    Within a single card, re-adding the same collection id / file path / note across deep runs must
    NOT accumulate duplicates. For each identity (see ``content_identity_key``) we keep the FIRST
    occurrence's stable ``id`` (so existing update targets stay valid), refresh ``ts`` to the NEWEST
    seen, and prefer the freshest non-empty ``why``. Order follows first appearance. Because existing
    card content is passed before freshly-added items, an existing item always wins identity (its id
    is preserved) while picking up the newer ts/why from the re-add. Pure; never raises.
    """
    try:
        by_key: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for it in items:
            key = content_identity_key(it)
            if key not in by_key:
                by_key[key] = dict(it)
                order.append(key)
                continue
            cur = by_key[key]
            cur_ts = float(cur.get("ts") or 0.0)
            new_ts = float(it.get("ts") or 0.0)
            new_why = (it.get("why") or "").strip() if isinstance(it.get("why"), str) else ""
            if new_ts >= cur_ts:
                cur["ts"] = it.get("ts", cur.get("ts"))
                if new_why:
                    cur["why"] = it.get("why")
            elif not ((cur.get("why") or "").strip()) and new_why:
                cur["why"] = it.get("why")
        return [by_key[key] for key in order]
    except Exception:  # noqa: BLE001
        return items


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


def _preview_of(text: str, *, max_chars: int = 160) -> str:
    """Short single-line snippet of ``text`` (~``max_chars``) for the consolidator LLM only.

    Collapses all whitespace runs to single spaces so the preview is one compact line, then caps at
    ``max_chars``. Never raises (returns "" on any failure).
    """
    try:
        flat = re.sub(r"\s+", " ", str(text or "")).strip()
        return flat[:max_chars]
    except Exception:  # noqa: BLE001
        return ""


def render_card_content_blocks(
    card: Dict[str, Any],
    resolvers: Dict[str, Any],
    *,
    task_kws: Set[str],
    max_refs: int = MAX_CARD_REFS,
    max_ref_chars: int = MAX_CARD_REF_CHARS,
    max_ref_item_chars: int = MAX_CARD_REF_ITEM_CHARS,
) -> List[Dict[str, Any]]:
    """Resolve a card's ``content`` items into structured ITEM BLOCKS. Never raises.

    The structured sibling of ``render_card_content``: same recency-bounded ranking and the same
    FRESH per-item resolution + char budgeting, but it returns one dict PER surviving item instead
    of flattened lines. Each block carries::

        {"id": str, "type": "file|collection|conversation|query|note", "why": str,
         "locator": dict,            # the typed reference pointer (for pointer materialization)
         "text": str,               # resolved VERBATIM rendered content (post-truncation), as today
         "preview": str,            # short single-line snippet of text, for the consolidator LLM
         "pointer_eligible": bool}  # True ONLY for type=="file" (a worker can re-read a file itself)

    ``render_card_content`` builds its lines from these blocks, so the two stay byte-for-byte in
    sync. The blocks additionally power the consolidating filter (which selects item ids) and the
    deep preamble's paste-vs-pointer materialization (``locator`` + ``pointer_eligible``).

    ``resolvers`` is a ``{type: ReferenceResolver}`` registry (see ``reference_resolver.py``).
    ``task_kws`` is the tokenized task text (use ``tokenize()``), used to rank which items resolve.
    """
    blocks: List[Dict[str, Any]] = []
    try:
        content = normalize_content(card.get("content"))
        if not content:
            return blocks
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
            blocks.append({
                "id": item.get("id", ""),
                "type": itype,
                "why": why,
                "locator": locator if isinstance(locator, dict) else {},
                "text": rendered,
                "preview": _preview_of(rendered),
                # Only a file can be re-read fresh by the deep worker's own fs tools, so only a file
                # item may ever be delivered as a POINTER instead of pasted content.
                "pointer_eligible": (itype == "file"),
            })
    except Exception:  # noqa: BLE001
        return blocks
    return blocks


def render_block_lines(block: Dict[str, Any]) -> List[str]:
    """Render ONE content block to its context lines (header + indented body).

    THE single layout authority for a content item: ``  - (<type>) <why>`` header followed by the
    block's VERBATIM ``text`` indented six spaces per line. Both ``render_card_content`` (which joins
    these across a card's blocks) and the prune-by-removal logic in the hybrid consolidator rely on
    this exact formatting, so the per-item fragment they reconstruct is a verbatim substring of the
    card's rendered section. Keep this the ONE place that decides item line layout.
    """
    itype = block.get("type", "note")
    why = block.get("why", "")
    rendered = block.get("text", "")
    lines = [f"  - ({itype})" + (f" {why}" if why else "")]
    for rl in rendered.splitlines() or [rendered]:
        lines.append(f"      {rl}")
    return lines


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

    Implemented on top of ``render_card_content_blocks`` (the structured form): each block's
    verbatim ``text`` is flattened back into the ``  - (type) why`` header + indented body lines, so
    this function's output is byte-for-byte identical to the prior inline implementation.

    ``resolvers`` is a ``{type: ReferenceResolver}`` registry (see ``reference_resolver.py``).
    ``task_kws`` is the tokenized task text (use ``tokenize()``), used to rank which items resolve.
    """
    lines: List[str] = []
    try:
        blocks = render_card_content_blocks(
            card,
            resolvers,
            task_kws=task_kws,
            max_refs=max_refs,
            max_ref_chars=max_ref_chars,
            max_ref_item_chars=max_ref_item_chars,
        )
        for block in blocks:
            lines.extend(render_block_lines(block))
    except Exception:  # noqa: BLE001
        return lines
    return lines
