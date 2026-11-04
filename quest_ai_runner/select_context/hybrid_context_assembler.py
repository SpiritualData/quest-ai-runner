"""HybridContextAssembler: QAR's binding of the hub's hybrid assembler (``hubofhubs.recall.hybrid_assembler``).

The assembler is the hub's: it runs a keyword arm and a vector arm in parallel, fuses their results by weighted reciprocal
rank, honours the caller's assembly deadline with a partial result, and (when a provider is wired) makes ONE consolidating
model pass over the merged card set. It reads no environment and holds no prompt. What stays with QAR is the consolidating
pass itself: ``core.card_filter.consolidate_context`` carries QAR's registered prompt, and it is looked up on every call (so a
test, or a lane, that replaces it is honoured), plus the declaration that this class is the ``hybrid`` implementation of
``select-context``.
"""
from __future__ import annotations

from typing import Any, Dict, List

from esoi import implementation
from hubofhubs.recall import hybrid_assembler as hub
from hubofhubs.recall.hybrid_assembler import (  # noqa: F401  (same objects, old import path)
    CONSOLIDATE_MIN_REMAINING_SECONDS,
    RRF_K,
    VECTOR_RANK_WEIGHT,
    arm_scores,
    fused_card_order,
)


def consolidate(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """QAR's consolidating pass, resolved when it is called (``core.card_filter`` imports the adapters' prompts, not the other way)."""
    from ..core import card_filter
    return card_filter.consolidate_context(*args, **kwargs)


@implementation("select-context", "hybrid")
class HybridContextAssembler(hub.HybridContextAssembler):
    """The hub's hybrid assembler with QAR's consolidating pass.

    Takes the hub's constructor arguments (``keyword``, ``vector``, then ``model_provider``, ``model``, ``consolidate``).
    """

    def __init__(self, keyword: Any, vector: Any, **settings: Any) -> None:
        super().__init__(keyword, vector, consolidate_fn=consolidate, **settings)
