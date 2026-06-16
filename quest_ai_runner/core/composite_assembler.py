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
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Return concatenated context from all assemblers. Never raises."""
        from .adapters import AssembledContext

        parts: List[str] = []
        card_ids: List[str] = []
        stale: List[str] = []
        hint: Optional[str] = None
        for a in self._assemblers:
            try:
                if _accepts_meta(a):
                    result = a.assemble(task_text, meta=meta)
                else:
                    result = a.assemble(task_text)
                if result.context_view:
                    parts.append(result.context_view)
                card_ids.extend(result.card_ids or [])
                stale.extend(result.stale or [])
                if hint is None and result.model_tier_hint:
                    hint = result.model_tier_hint
            except Exception:
                pass
        return AssembledContext(
            context_view="\n\n".join(parts),
            model_tier_hint=hint,
            card_ids=card_ids,
            stale=stale,
        )

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Call record() on each assembler best-effort. Never raises."""
        for a in self._assemblers:
            try:
                a.record(task_text, outcome)
            except Exception:
                pass
