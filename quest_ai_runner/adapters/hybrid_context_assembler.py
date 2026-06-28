"""HybridContextAssembler — fuses keyword/IDF and vector assemblers.

This is an RRF-style complementary fusion (Reciprocal Rank Fusion in spirit):
keyword/IDF search catches exact identifiers and symbols; vector search catches
semantics and paraphrase.  Running both in parallel and unioning their results
gives higher recall than either alone.

DESIGN
------
Both assemblers run IN PARALLEL (via ThreadPoolExecutor).  Their outputs are
fused into a single AssembledContext:

* ``context_view`` — keyword cards section + vector hits section, clearly
  labelled.  When one assembler returns empty its section is omitted.
* ``card_ids``     — union of both assemblers' card_ids (deduped, keyword first).
* ``stale``        — union of both assemblers' stale lists (deduped).

FALLBACK
--------
If BOTH assemblers return an empty context_view the hybrid also returns an
empty AssembledContext, and the caller falls back to plain Claude Code (the
never-worse guarantee).

RECORD
------
``record()`` forwards to both assemblers so both auto-accumulate over time.
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Dict, List, Optional

from ..core.adapters import AssembledContext, ContextAssemblerBase, ContextAssembler

logger = logging.getLogger(__name__)


class HybridContextAssembler(ContextAssemblerBase):
    """Fuses a keyword/IDF assembler and a vector assembler in parallel.

    This is the recommended entry point when both a ``FileContextStore`` and a
    ``VectorContextAssembler`` (or any pair of ``ContextAssembler``-compatible
    objects) are available.  The two assemblers are complementary:

    * **Keyword/IDF** (``FileContextStore``) — exact identifier and symbol matching.
    * **Vector** (``VectorContextAssembler``) — semantic / paraphrase matching.

    Parameters
    ----------
    keyword:
        A ``ContextAssembler``-compatible object for keyword/IDF retrieval
        (typically a ``FileContextStore``).
    vector:
        A ``ContextAssembler``-compatible object for vector retrieval
        (typically a ``VectorContextAssembler``).
    model_provider:
        Optional ``MultiProvider`` (or any ``ModelProvider``) used for the ONE
        consolidating LLM pass over the merged card set. When ``None`` the merge
        stays purely mechanical (today's behavior).
    model:
        Optional resolved model id for that consolidation call (e.g. the
        "balanced" tier). Never a hardcoded id.
    consolidate:
        When True (default) and a ``model_provider`` is wired and at least one
        merged card carries structured ``items``, run ``consolidate_context`` to
        drop/rerank cards across arms and prune their content items, then rebuild
        ``context_view`` verbatim from the survivors. Any failure (or no provider /
        no items) falls back to the mechanical merge (the never-worse guarantee).
    """

    def __init__(
        self,
        keyword: ContextAssembler,
        vector: ContextAssembler,
        *,
        model_provider: Optional[Any] = None,
        model: Optional[str] = None,
        consolidate: bool = True,
    ) -> None:
        self._keyword = keyword
        self._vector = vector
        self._model_provider = model_provider
        self._model = model
        self._consolidate = consolidate

    # ------------------------------------------------------------------
    # ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Run both assemblers IN PARALLEL and fuse their results.  Never raises."""
        try:
            return self._assemble_inner(task_text, meta=meta)
        except Exception:
            logger.debug("HybridContextAssembler.assemble failed", exc_info=True)
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Forward write-back to both assemblers so both compound.  Never raises."""
        for asm in (self._keyword, self._vector):
            try:
                asm.record(task_text, outcome)
            except Exception:
                logger.debug(
                    "HybridContextAssembler.record: assembler.record failed",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assemble_inner(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        kw_result: AssembledContext = AssembledContext()
        vec_result: AssembledContext = AssembledContext()

        def _run_keyword() -> AssembledContext:
            try:
                return self._keyword.assemble(task_text, meta=meta)
            except Exception:
                return AssembledContext()

        def _run_vector() -> AssembledContext:
            try:
                return self._vector.assemble(task_text, meta=meta)
            except Exception:
                return AssembledContext()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_kw = pool.submit(_run_keyword)
                fut_vec = pool.submit(_run_vector)
                try:
                    kw_result = fut_kw.result()
                except Exception:
                    kw_result = AssembledContext()
                try:
                    vec_result = fut_vec.result()
                except Exception:
                    vec_result = AssembledContext()
        except Exception:
            # ThreadPoolExecutor failed: run serially.
            kw_result = _run_keyword()
            vec_result = _run_vector()

        # Fuse: both empty -> empty (fall back to baseline).
        if not kw_result.context_view and not vec_result.context_view:
            return AssembledContext()

        # Build fused context_view with clearly labelled sections.
        view_parts: List[str] = []
        if kw_result.context_view:
            view_parts.append(
                f"## Keyword context (IDF cards)\n\n{kw_result.context_view}"
            )
        if vec_result.context_view:
            view_parts.append(
                f"## Vector context (semantic hits)\n\n{vec_result.context_view}"
            )
        combined_view = "\n\n---\n\n".join(view_parts)

        # Union card_ids (keyword first, then vector), deduped preserving order.
        seen: set = set()
        merged_ids: List[str] = []
        for cid in kw_result.card_ids + vec_result.card_ids:
            if cid not in seen:
                seen.add(cid)
                merged_ids.append(cid)

        # Union stale lists, deduped.
        seen_stale: set = set()
        merged_stale: List[str] = []
        for s in kw_result.stale + vec_result.stale:
            if s not in seen_stale:
                seen_stale.add(s)
                merged_stale.append(s)

        # Use the non-None model_tier_hint (keyword takes priority if both set).
        tier_hint = kw_result.model_tier_hint or vec_result.model_tier_hint

        # Merge sources from both arms (keyword first, then vector), preserving order.
        merged_sources: List[dict] = []
        seen_source_keys: set = set()
        for src in (getattr(kw_result, "sources", None) or []) + \
                   (getattr(vec_result, "sources", None) or []):
            key = (src.get("adapter"), src.get("label"))
            if key not in seen_source_keys:
                seen_source_keys.add(key)
                merged_sources.append(src)

        # Merge card_metadata from both arms (keyword first, then vector), deduped by id.
        merged_metadata: List[dict] = []
        seen_card_ids: set = set()
        for meta in (getattr(kw_result, "card_metadata", None) or []) + \
                    (getattr(vec_result, "card_metadata", None) or []):
            card_id = meta.get("id")
            if card_id and card_id not in seen_card_ids:
                seen_card_ids.add(card_id)
                merged_metadata.append(meta)

        mechanical = AssembledContext(
            context_view=combined_view,
            model_tier_hint=tier_hint,
            card_ids=merged_ids,
            stale=merged_stale,
            sources=merged_sources,
            card_metadata=merged_metadata,
        )

        # --- ONE consolidating LLM pass over the MERGED card set --------------------------------
        # Drops tangential/redundant cards across arms, reranks them, and prunes which content items
        # survive (content stays VERBATIM, the LLM selects ids only). Engages ONLY when a provider is
        # wired AND at least one merged card carries structured ``items``. On no-provider / no-items /
        # any failure -> the mechanical merge above (the never-worse guarantee), unchanged.
        if (
            self._consolidate
            and self._model_provider is not None
            and any((m.get("items") for m in merged_metadata))
        ):
            consolidated = self._consolidate_merged(task_text, merged_metadata, mechanical)
            if consolidated is not None:
                return consolidated
        return mechanical

    def _consolidate_merged(
        self,
        task_text: str,
        merged_metadata: List[dict],
        mechanical: AssembledContext,
    ) -> Optional[AssembledContext]:
        """Run the consolidating filter and rebuild from survivors. None on any failure/empty.

        Returns a new ``AssembledContext`` whose ``context_view`` is rebuilt from each surviving
        card's VERBATIM ``rendered_section`` (its whole rendered block: summary + file listings +
        content + conventions, NOT just its content items), in the consolidator's order. When the
        consolidator pruned some of a card's content items, those items' rendered fragments are
        REMOVED from the verbatim section by string match (never re-synthesized); a fragment that
        cannot be located is left intact rather than risk corrupting the section. A card with no
        ``rendered_section`` (e.g. a stub assembler) falls back to the old item-only rebuild under a
        ``### <title>`` header. ``card_metadata``/``card_ids`` are set to the consolidated set, each
        kept item carries its ``deliver`` tag, and each surviving card's ``rendered_section`` is
        updated to the pruned text so the deep preamble stays consistent. Returns None (caller keeps
        the mechanical merge) when nothing survives or anything goes wrong. Never raises.
        """
        try:
            # Local import keeps the core<-adapter dependency one-directional and avoids any cycle.
            from ..core.card_filter import consolidate_context
            from .card_content_render import render_block_lines

            def _block_fragment(blk: dict) -> str:
                """The exact text a content block contributed to a rendered section (header + indented
                body), so prune-by-removal is a pure verbatim-substring match. Mirrors how
                ``render_card_content`` lays a block out (both go through ``render_block_lines``)."""
                return "\n".join(render_block_lines(blk))

            # EVERY merged card participates (not only item-bearing ones): a file-only keyword card
            # carries its value in its summary + file listings, so the consolidator must be able to
            # keep/drop/rerank it too. Item-less cards go in with items=[] and a card-level preview.
            consolidator_input: List[dict] = []
            for m in merged_metadata:
                items = m.get("items") or []
                entry: Dict[str, Any] = {
                    "id": m.get("id", ""),
                    "title": m.get("title", ""),
                    "items": [
                        {"id": it.get("id", ""), "type": it.get("type", "note"),
                         "why": it.get("why", ""), "preview": it.get("preview", "")}
                        for it in items
                    ],
                }
                if not items:
                    # Give the LLM something to judge a file-only card by: its title/summary, then a
                    # short slice of its verbatim rendered section.
                    rs = (m.get("rendered_section") or "")
                    entry["preview"] = (m.get("title") or "") or rs[:160]
                consolidator_input.append(entry)
            if not consolidator_input:
                return None

            verdict = consolidate_context(
                task_text, consolidator_input,
                model_provider=self._model_provider, model=self._model,
            )
            if not verdict:
                return None

            meta_by_id = {m.get("id", ""): m for m in merged_metadata}
            view_parts: List[str] = []
            new_metadata: List[dict] = []
            new_ids: List[str] = []
            for entry in verdict:
                cid = entry.get("card_id", "")
                m = meta_by_id.get(cid)
                if m is None:
                    continue
                original_items = m.get("items") or []
                item_by_id = {it.get("id", ""): it for it in original_items}
                surviving: List[dict] = []
                for sel in (entry.get("items") or []):
                    blk = item_by_id.get(sel.get("item_id", ""))
                    if blk is None:
                        continue
                    blk = dict(blk)
                    # Record the per-item delivery decision for the deep preamble. The planner/answer
                    # path still pastes everything below regardless of this tag.
                    blk["deliver"] = sel.get("deliver", "paste")
                    surviving.append(blk)

                rendered = m.get("rendered_section")
                if rendered:
                    # Start from the VERBATIM section. Prune ONLY when this card had items and the
                    # kept set is a strict subset; remove each pruned item's exact rendered fragment.
                    section = rendered
                    if original_items and len(surviving) < len(original_items):
                        kept_ids = {b.get("id", "") for b in surviving}
                        for ob in original_items:
                            if ob.get("id", "") in kept_ids:
                                continue
                            frag = _block_fragment(ob)
                            if frag and frag in section:
                                section = section.replace(frag, "", 1)
                            else:
                                # Fallback: try the raw resolved text; if neither is found, leave the
                                # section intact (never corrupt it) rather than guess.
                                raw = ob.get("text", "")
                                if raw and raw in section:
                                    section = section.replace(raw, "", 1)
                    part = section
                else:
                    # No rendered_section (e.g. a stub assembler): rebuild from surviving items under
                    # a title header, which is the prior behavior.
                    title = m.get("title") or cid or "context"
                    body = "\n\n".join(b.get("text", "") for b in surviving if b.get("text"))
                    part = f"### {title}\n\n{body}" if body else f"### {title}"

                if not (part or "").strip():
                    continue
                view_parts.append(part)
                new_m = dict(m)
                new_m["items"] = surviving
                # Keep rendered_section consistent with the consolidated view (pruned), so the deep
                # preamble materializes from the same surviving text.
                if rendered:
                    new_m["rendered_section"] = part
                new_metadata.append(new_m)
                new_ids.append(cid)

            rebuilt_view = "\n\n---\n\n".join(p for p in view_parts if p)
            if not rebuilt_view or not new_metadata:
                return None

            return AssembledContext(
                context_view=rebuilt_view,
                model_tier_hint=mechanical.model_tier_hint,
                card_ids=new_ids,
                stale=mechanical.stale,
                sources=mechanical.sources,
                card_metadata=new_metadata,
            )
        except Exception:
            logger.debug("HybridContextAssembler consolidation failed", exc_info=True)
            return None
