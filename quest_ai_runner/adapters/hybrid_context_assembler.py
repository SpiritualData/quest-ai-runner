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

DEADLINE (partial results)
--------------------------
When the caller passes ``meta["assembly_deadline"]`` (a ``time.monotonic()``
timestamp), the fuse step becomes deadline-aware: an arm that has not finished
by the deadline is SKIPPED and the arm(s) that completed are fused as a partial
result (``AssembledContext.partial=True``) so a slow arm never costs the caller
its whole context budget.  An arm is skipped ONLY when a completed arm actually
holds content: if no completed arm has any (neither finished, or the finished
arm(s) crashed or came back empty) the hybrid blocks for the missing arm(s)
exactly as before -- returning an early empty result would look like "assembly
found nothing" and defeat the caller's own timeout/late-recovery handling for
the true zero-results case.  The consolidating LLM pass is bypassed
when the result is partial or the remaining budget could not absorb it (the
fails-never-worse philosophy of ``core/card_filter.py``).  Without a deadline in
``meta``, behavior is unchanged.

RECORD
------
``record()`` forwards to both assemblers so both auto-accumulate over time.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Dict, List, Optional

from ..core.adapters import AssembledContext, ContextAssemblerBase, ContextAssembler

logger = logging.getLogger(__name__)

# Minimum seconds that must remain before ``meta["assembly_deadline"]`` for the consolidating
# LLM pass to be attempted at all; with less than this left the mechanical merge is returned
# directly (bypassing consolidation can only cost polish, never content -- fails never worse).
CONSOLIDATE_MIN_REMAINING_SECONDS = 1.0


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

    def render_card(self, card_id: str, *, meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Fetch ONE card by id (the brain's mid-loop ``{"card": <id>}`` read) from whichever arm
        can render it. Tries the keyword arm first (it owns the CardRepository), then the vector arm.
        Never raises."""
        for asm in (self._keyword, self._vector):
            fn = getattr(asm, "render_card", None)
            if not callable(fn):
                continue
            try:
                out = fn(card_id, meta=meta)
            except TypeError:
                try:
                    out = fn(card_id)
                except Exception:
                    out = None
            except Exception:
                out = None
            if out:
                return out
        return None

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

        # Optional soft deadline: ``meta["assembly_deadline"]`` is a ``time.monotonic()``
        # timestamp set by the caller (e.g. the Orchestrator's turn-start assembly, slightly
        # under its own hard collect timeout).  Absent or malformed -> no deadline, prior
        # behavior byte-for-byte.
        deadline: Optional[float] = None
        try:
            raw_deadline = (meta or {}).get("assembly_deadline")
            if raw_deadline is not None:
                deadline = float(raw_deadline)
        except Exception:
            deadline = None

        partial = False
        pool = None
        try:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            fut_kw = pool.submit(_run_keyword)
            fut_vec = pool.submit(_run_vector)
            arm_results: Dict[str, Optional[AssembledContext]] = {}
            for name, fut in (("keyword", fut_kw), ("vector", fut_vec)):
                remaining: Optional[float] = None
                if deadline is not None:
                    remaining = max(deadline - time.monotonic(), 0.0)
                try:
                    arm_results[name] = fut.result(timeout=remaining)
                except concurrent.futures.TimeoutError:
                    arm_results[name] = None  # not finished by the deadline
                except Exception:
                    arm_results[name] = AssembledContext()
            arm_futures = {"keyword": fut_kw, "vector": fut_vec}
            missed = [name for name in ("keyword", "vector") if arm_results[name] is None]
            # An arm only counts as "finished" for the skip decision when it finished WITH
            # content: an arm that crashed or legitimately found nothing yields an empty
            # AssembledContext, and skipping the slow arm on the strength of an EMPTY one
            # would return an early empty "partial" that reads as "assembly completed and
            # found nothing" -- poisoning the caller's cache and defeating its own hard
            # timeout + late-recovery path, which is the correct owner of the true
            # zero-results case.
            completed_with_content = any(
                res is not None and res.context_view for res in arm_results.values()
            )
            if missed and not completed_with_content:
                # Deadline expired with no completed arm holding content (neither arm
                # finished, or the finished arm(s) came back empty): block for the missing
                # arm(s), the pre-deadline behavior.
                for name in missed:
                    try:
                        arm_results[name] = arm_futures[name].result()
                    except Exception:
                        arm_results[name] = AssembledContext()
            elif missed:
                skipped = missed[0]
                partial = True
                logger.warning(
                    "HybridContextAssembler: %s arm missed the assembly deadline and was "
                    "skipped; fusing the completed arm as a partial result", skipped)
            kw_result = arm_results["keyword"] or AssembledContext()
            vec_result = arm_results["vector"] or AssembledContext()
        except Exception:
            # ThreadPoolExecutor failed: run serially.
            kw_result = _run_keyword()
            vec_result = _run_vector()
        finally:
            if pool is not None:
                try:
                    # wait=False: a skipped arm's thread finishes on its own; never block on it.
                    pool.shutdown(wait=False)
                except Exception:
                    pass

        # Fuse: both empty -> empty (fall back to baseline).
        if not kw_result.context_view and not vec_result.context_view:
            return AssembledContext(partial=partial)

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

        # Merge card_metadata from both arms (keyword first, then vector), deduped by id. NOTE:
        # loop variable named ``card_meta`` (NOT ``meta``) -- this function's ``meta`` PARAMETER
        # (the caller's assemble() meta, e.g. carrying ``recent_item_usage``) must survive past
        # this loop so it can be threaded into ``_consolidate_merged`` below; shadowing it here
        # silently emptied that hint in an earlier version of this code.
        merged_metadata: List[dict] = []
        seen_card_ids: set = set()
        for card_meta in (getattr(kw_result, "card_metadata", None) or []) + \
                    (getattr(vec_result, "card_metadata", None) or []):
            card_id = card_meta.get("id")
            if card_id and card_id not in seen_card_ids:
                seen_card_ids.add(card_id)
                merged_metadata.append(card_meta)

        mechanical = AssembledContext(
            context_view=combined_view,
            model_tier_hint=tier_hint,
            card_ids=merged_ids,
            stale=merged_stale,
            sources=merged_sources,
            card_metadata=merged_metadata,
            partial=partial,
        )

        # --- ONE consolidating LLM pass over the MERGED card set --------------------------------
        # Drops tangential/redundant cards across arms, reranks them, and prunes which content items
        # survive (content stays VERBATIM, the LLM selects ids only). Engages ONLY when a provider is
        # wired AND at least one merged card carries structured ``items``. On no-provider / no-items /
        # any failure -> the mechanical merge above (the never-worse guarantee), unchanged.
        # BUDGET GATE: a partial result means the deadline already expired, and even a full fuse
        # skips consolidation when less than CONSOLIDATE_MIN_REMAINING_SECONDS of budget is left --
        # an LLM pass that would blow the caller's budget is worse than the mechanical merge
        # (fails never worse, same philosophy as core/card_filter.py).
        if (
            self._consolidate
            and self._model_provider is not None
            and not partial
            and (deadline is None
                 or (deadline - time.monotonic()) >= CONSOLIDATE_MIN_REMAINING_SECONDS)
            and any((m.get("items") for m in merged_metadata))
        ):
            consolidated = self._consolidate_merged(task_text, merged_metadata, mechanical, meta=meta)
            if consolidated is not None:
                return consolidated
        return mechanical

    def _consolidate_merged(
        self,
        task_text: str,
        merged_metadata: List[dict],
        mechanical: AssembledContext,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[AssembledContext]:
        """Run the consolidating filter and rebuild from survivors. None on any failure/empty.

        Returns a new ``AssembledContext`` whose ``context_view`` is rebuilt from each surviving
        card's VERBATIM ``rendered_section`` (its whole rendered block: summary + file listings +
        content + conventions, NOT just its content items), in the consolidator's order. When the
        consolidator pruned some of a card's content items, those items' rendered fragments are
        REMOVED from the verbatim section by string match (never re-synthesized); a fragment that
        cannot be located is left intact rather than risk corrupting the section. Beyond pruning,
        when the consolidator returns a card's surviving items in a DIFFERENT order than they
        appear in the verbatim section (e.g. because a ``recent_item_usage`` hint moved a
        previously-useful item to the front), the item region of the section is REBUILT in the
        consolidator's order -- see ``_reordered_section``. A card with no ``rendered_section``
        (e.g. a stub assembler) falls back to the old item-only rebuild under a ``### <title>``
        header, which already follows the consolidator's item order. ``card_metadata``/``card_ids``
        are set to the consolidated set, each kept item carries its ``deliver`` tag, and each
        surviving card's ``rendered_section`` is updated to the pruned/reordered text so the deep
        preamble stays consistent. Returns None (caller keeps the mechanical merge) when nothing
        survives or anything goes wrong. Never raises.

        ``meta`` is the same dict the caller's ``assemble(task_text, meta=...)`` received; when it
        carries ``meta["recent_item_usage"]`` (``{card_id: [item_id, ...]}``, see
        ``core.recent_context.build_item_usage_hint``), that hint rides the consolidation prompt so
        the LLM prefers/orders-first the items a similar past input already found useful. A plain
        HINT, never a hard override -- see ``core.card_filter.consolidate_context``.
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

            def _reordered_section(section: str, original_items: List[dict], surviving: List[dict]) -> str:
                """Rebuild the CONTIGUOUS item region of ``section`` in ``surviving``'s order.

                ``render_card_content`` lays a card's items out back-to-back with no separator
                between them (each block's own lines already start with its ``- (type)`` header),
                so the ORIGINAL item region is exactly ``"\\n".join(_block_fragment(it) for it in
                original_items)``. When that exact substring is found in ``section`` and pruning/
                reordering actually changed anything, replace it with the survivors' fragments
                joined the same way, in ``surviving``'s order. Falls back to ``section`` unchanged
                (never corrupts it) when the original region cannot be located verbatim -- e.g. a
                deployment that renders items differently than this module expects.
                """
                if not original_items or not surviving:
                    return section
                original_ids = [ob.get("id", "") for ob in original_items]
                surviving_ids = [sb.get("id", "") for sb in surviving]
                if original_ids == surviving_ids:
                    return section  # nothing pruned AND nothing reordered
                original_region = "\n".join(_block_fragment(ob) for ob in original_items)
                if not original_region or original_region not in section:
                    return section
                new_region = "\n".join(_block_fragment(sb) for sb in surviving)
                return section.replace(original_region, new_region, 1)

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

            recent_item_usage = (meta or {}).get("recent_item_usage") or {}
            verdict = consolidate_context(
                task_text, consolidator_input,
                model_provider=self._model_provider, model=self._model,
                recent_item_usage=recent_item_usage,
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
                    # Start from the VERBATIM section. Prune/reorder ONLY when this card had items
                    # and either some were dropped or the consolidator returned a different order
                    # (e.g. a recent_item_usage hint moved a previously-useful item to the front).
                    section = rendered
                    original_ids = [ob.get("id", "") for ob in original_items]
                    surviving_ids = [sb.get("id", "") for sb in surviving]
                    if original_items and original_ids != surviving_ids:
                        # ONE clean substitution: the whole original item region -> the survivors'
                        # fragments in the CONSOLIDATOR's order, both pruning and reordering in a
                        # single string operation (see _reordered_section). This only fires when the
                        # region is found as an exact contiguous substring; otherwise fall back to
                        # the old per-fragment removal below (prune only, original order preserved --
                        # never corrupts the section, just skips reordering).
                        reordered = _reordered_section(section, original_items, surviving)
                        if reordered != section:
                            section = reordered
                        else:
                            kept_ids = {b.get("id", "") for b in surviving}
                            for ob in original_items:
                                if ob.get("id", "") in kept_ids:
                                    continue
                                frag = _block_fragment(ob)
                                if frag and frag in section:
                                    section = section.replace(frag, "", 1)
                                else:
                                    # Fallback: try the raw resolved text; if neither is found, leave
                                    # the section intact (never corrupt it) rather than guess.
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
