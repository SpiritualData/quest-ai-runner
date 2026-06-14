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
    """

    def __init__(
        self,
        keyword: ContextAssembler,
        vector: ContextAssembler,
    ) -> None:
        self._keyword = keyword
        self._vector = vector

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

        return AssembledContext(
            context_view=combined_view,
            model_tier_hint=tier_hint,
            card_ids=merged_ids,
            stale=merged_stale,
        )
