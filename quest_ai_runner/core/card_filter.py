"""Card filter: QAR's prompts, registrations and entry points over the hub's card selection.

The mechanics (the card-level relevance pass, the batched within-card file ranking, the consolidating pass over a merged card
set, the selection memo and the tolerant JSON extraction) are the hub's, ``hubofhubs.recall.card_selection``. What stays here
is what knows QAR: the three prompts as registered prompt specs (the standard texts are the hub's, declared here so each carries
its solution, metric and tier), the prompt budget that trims a long brief to the gist a judge needs, and the entry points the
rest of QAR calls with those bound.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from hubofhubs.ports.model import ModelProvider
from hubofhubs.recall import card_selection as hub
from hubofhubs.recall.card_selection import CardMetadata, stable_card_order  # noqa: F401  (same objects, old import path)

from . import prompt_budget
from .prompt_specs import declare, render_prompt

# The tolerant JSON extraction is used across QAR under this name; it is the hub's function.
_extract_json = hub.extract_json

CONSOLIDATE_SPEC = declare(
    "select-context.consolidate", solution="select-context", metric="metric-select-context",
    tier="balanced", cache_layer="none", template=hub.CONSOLIDATE_TEMPLATE)
RANK_FILES_SPEC = declare(
    "select-context.rank-files", solution="select-context", metric="metric-select-context",
    tier="balanced", cache_layer="none", template=hub.RANK_FILES_TEMPLATE)
# The card-level pass was an inline prompt; it is registered with its text unchanged (no em-dash rule appended to it).
CARD_SCORES_SPEC = declare(
    "select-context.card-scores", solution="select-context", metric="metric-select-context",
    tier="balanced", cache_layer="none", template=hub.CARD_SCORES_TEMPLATE, writes_text=False)


def consolidate_prompt(task: str, cards_block: str) -> str:
    return render_prompt("select-context.consolidate", task=task, cards_block=cards_block)


def rank_files_prompt(task: str, cards_block: str) -> str:
    return render_prompt("select-context.rank-files", task=task, cards_block=cards_block)


def card_scores_prompt(task: str, card_list: str) -> str:
    return render_prompt("select-context.card-scores", task=task, card_list=card_list)


def excerpt_of(task: str) -> str:
    """A judge needs the gist of the request, never the whole brief (``prompt_budget``)."""
    return prompt_budget.decision_excerpt(task)


PROMPTS = hub.SelectionPrompts(consolidate=consolidate_prompt, rank_files=rank_files_prompt,
                               card_scores=card_scores_prompt, excerpt=excerpt_of)


def consolidate_context(
    task: str,
    cards: List[Dict[str, Any]],
    *,
    model_provider: Optional[ModelProvider] = None,
    model: Optional[str] = None,
    recent_item_usage: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """ONE holistic LLM pass over the merged card set: drop, rerank, and prune content items (the hub's pass, QAR's prompt).

    ``recent_item_usage`` is the ``{card_id: [item_id, ...]}`` hint built by ``core.recent_context.build_item_usage_hint``.
    Graceful: keep-all when no provider is wired or anything fails. Never raises.
    """
    return hub.consolidate_context(task, cards, model_provider=model_provider, model=model,
                                   recent_item_usage=recent_item_usage, prompts=PROMPTS)


def filter_cards_by_relevance(
    task: str,
    candidate_cards: List[Dict[str, Any]],
    *,
    model_provider: Optional[ModelProvider] = None,
    model: Optional[str] = None,
) -> List[CardMetadata]:
    """LLM relevance filtering of context cards (the hub's two-stage pass, QAR's prompts). Ordered by card id, stable."""
    return hub.filter_cards_by_relevance(task, candidate_cards, model_provider=model_provider, model=model, prompts=PROMPTS)
