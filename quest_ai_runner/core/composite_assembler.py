"""Composite ContextAssembler -- stdlib only."""
import inspect
from typing import Any, Dict, List, Optional


def _accepts_meta(assembler: Any) -> bool:
    try:
        sig = inspect.signature(assembler.assemble)
        return "meta" in sig.parameters
    except Exception:
        return False


class CompositeContextAssembler:
    """Wraps multiple ContextAssembler instances into one.

    assemble() calls each assembler, concatenates their context_view strings
    (non-empty ones only), and merges their card_ids and stale lists.
    record() calls each assembler best-effort (never raises).

    Usage::

        assembler = CompositeContextAssembler([file_store, turn_store])
        cfg = RunnerConfig(..., context_assembler=assembler)
    """

    def __init__(self, assemblers: List[Any]):
        self._assemblers = assemblers

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, on_event=None
    ) -> Any:
        """Return concatenated context from all assemblers. Never raises.

        ``on_event``, if given, is called after each inner assembler completes with
        ``("arm_done", {"assembler": ..., "cards_found": ..., "context_chars": ...})``.
        ``on_event`` is NOT forwarded to inner assemblers; only this composite emits it.
        """
        from .adapters import AssembledContext
        import logging

        _log = logging.getLogger(__name__)
        parts: List[str] = []
        card_ids: List[str] = []
        stale: List[str] = []
        hint: Optional[str] = None
        sources: List[Dict[str, Any]] = []
        card_metadata: List[Dict[str, Any]] = []

        for a in self._assemblers:
            try:
                if _accepts_meta(a):
                    result = a.assemble(task_text, meta=meta)
                else:
                    result = a.assemble(task_text)
                if result is None:
                    _log.warning(f"Assembler {type(a).__name__} returned None instead of AssembledContext")
                    continue
                if result.context_view:
                    parts.append(result.context_view)
                card_ids.extend(result.card_ids or [])
                stale.extend(result.stale or [])
                sources.extend(getattr(result, 'sources', None) or [])
                card_metadata.extend(getattr(result, 'card_metadata', None) or [])
                if hint is None and result.model_tier_hint:
                    hint = result.model_tier_hint
                if on_event is not None:
                    try:
                        on_event("arm_done", {
                            "assembler": type(a).__name__,
                            "cards_found": len(result.card_metadata or []),
                            "context_chars": len(result.context_view or ""),
                        })
                    except Exception:
                        pass
            except Exception as e:
                _log.debug(f"Assembler {type(a).__name__} failed: {type(e).__name__}: {e}", exc_info=True)

        return AssembledContext(
            context_view="\n\n".join(parts),
            model_tier_hint=hint,
            card_ids=card_ids,
            stale=stale,
            sources=sources,
            card_metadata=card_metadata,
        )

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Call record() on each assembler best-effort. Never raises."""
        for a in self._assemblers:
            try:
                a.record(task_text, outcome)
            except Exception:
                pass
