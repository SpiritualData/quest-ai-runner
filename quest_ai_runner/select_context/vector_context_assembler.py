"""VectorContextAssembler: QAR's binding of the hub's vector context assembler (``hubofhubs.recall.vector_assembler``).

The assembler is the hub's: parallel multi-query semantic search over a ``VectorStore``, the optional LLM query generation
and hit review, the confidence gate on raw similarity, the specificity-then-recency ranking, the stable render order, the
card-reference resolution, the cross-scope fence, the cold-start seed and the association capacity bound. It reads no
environment and holds no prompt of its own. What stays with QAR:

* the seeded-card scope ``CARD_SEED_SCOPE`` (its digest is part of every stored point, so the key QAR wrote under is kept);
* the seed ceiling, read from ``QAR_VECTOR_MAX_SEED_ITEMS`` (``default_max_seed_items``);
* the three prompts of its model steps as registered prompt specs (the standard texts are the hub's), and the prompt budget
  that trims a long brief to the gist query generation needs;
* the seed thread's name, and the declaration that this class is the ``vector`` implementation of ``select-context``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from esoi import implementation
from hubofhubs.recall import vector_assembler as hub
from hubofhubs.recall.vector_assembler import (  # noqa: F401  (same objects, old import path)
    MAX_SEED_ITEMS_DEFAULT,
    SNIPPET_MAX_CHARS,
    VECTOR_HIT_MAX_CARD_REF_CHARS,
    VECTOR_HIT_MAX_CARD_REFS,
    seed_source_takes_limit,
    snippet,
    task_slug,
)

from ..core import prompt_budget
from ..core.prompt_specs import declare, render_prompt
from ..core.vector_scopes import CARD_SEED_SCOPE

logger = logging.getLogger(__name__)


def default_max_seed_items() -> int:
    """The seed ceiling from the environment, else the hub's default. Never raises."""
    raw = os.getenv("QAR_VECTOR_MAX_SEED_ITEMS", "").strip()
    if not raw:
        return MAX_SEED_ITEMS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning("vector seed: QAR_VECTOR_MAX_SEED_ITEMS=%r is not an integer — using %d",
                       raw, MAX_SEED_ITEMS_DEFAULT)
        return MAX_SEED_ITEMS_DEFAULT
    return value if value >= 0 else MAX_SEED_ITEMS_DEFAULT


# The prompts of the assembler's three model steps. The text is the hub's; the spec carries QAR's solution, metric and tier
# (``caller``: the model is the assembler's configured ``query_model``). Not marked as writing text: the prompt is sent as is.
QUERIES_SPEC = declare(
    "index-by-meaning.queries", solution="index-by-meaning", metric="metric-index-by-meaning",
    tier="caller", cache_layer="none", template=hub.QUERIES_TEMPLATE, writes_text=False)
REVIEW_SPEC = declare(
    "index-by-meaning.review", solution="index-by-meaning", metric="metric-index-by-meaning",
    tier="caller", cache_layer="none", template=hub.REVIEW_TEMPLATE, writes_text=False)
ASSOCIATION_SPEC = declare(
    "index-by-meaning.association", solution="index-by-meaning", metric="metric-index-by-meaning",
    tier="caller", cache_layer="none", template=hub.ASSOCIATION_TEMPLATE, writes_text=False)


def queries_prompt(num_queries: int, task_text: str) -> str:
    return render_prompt("index-by-meaning.queries", num_queries=num_queries, task_text=task_text)


def review_prompt(task_text: str, items_text: str) -> str:
    return render_prompt("index-by-meaning.review", task_text=task_text, items_text=items_text)


def association_prompt(task_text: str, region_desc: str) -> str:
    return render_prompt("index-by-meaning.association", task_text=task_text, region_desc=region_desc)


PROMPTS = hub.VectorPrompts(queries=queries_prompt, review=review_prompt, association=association_prompt,
                            excerpt=prompt_budget.decision_excerpt)


@implementation("select-context", "vector")
class VectorContextAssembler(hub.VectorContextAssembler):
    """The hub's vector assembler under QAR's seed scope, seed ceiling, prompts and seed thread name.

    Takes the hub's constructor arguments (``vector_store`` first, then ``provider``, ``query_model``, ``llm_review``,
    ``num_queries``, ``top_k``, ``confidence_min_score``, ``max_in_view``, ``half_life_days``, ``max_associations``,
    ``max_seed_items``, ``seed_source``, ``seed_in_background``, ``reference_resolvers``, ``max_card_refs``,
    ``max_card_ref_chars``, ``clock``); ``max_seed_items=None`` takes ``default_max_seed_items()``.
    """

    def __init__(self, vector_store: Any, *, max_seed_items: Optional[int] = None, **settings: Any) -> None:
        super().__init__(
            vector_store,
            max_seed_items=default_max_seed_items() if max_seed_items is None else max_seed_items,
            seed_scope=CARD_SEED_SCOPE, prompts=PROMPTS, seed_thread_name="qar-vector-seed", **settings)
