"""QAR's binding of the hub's store merge: the card limit setting, the vector scope of card points and the vector folders.

``hubofhubs.recall.card_store_merge`` merges a narrow store into a wider one and reads no environment. These two functions
supply what only QAR knows: the wide store's card limit comes from ``QAR_MAX_CARDS``, card points live in the
``CARD_SEED_SCOPE`` partition of QAR's vector store, and that store keeps its files in ``qdrant`` and ``cards-qdrant`` folders
inside a card folder. The other names (``MergePlan``, ``item_id``, the statuses) are imported from the hub where they are used.
"""
from __future__ import annotations

from typing import Any, Optional

from hubofhubs.recall import card_store_merge as hub_merge
from hubofhubs.recall.card_store_merge import MergePlan

from ..core.vector_scopes import CARD_SEED_SCOPE
from .card_store_settings import ENV_MAX_CARDS, max_cards_from_env

VECTOR_DIRS = ("qdrant", "cards-qdrant")


def plan_merge(narrow_dir: str, wide_dir: str, *, vector_store: Any = None, prune_source: bool = False,
               max_cards: Optional[int] = None) -> MergePlan:
    """What merging ``narrow_dir`` into ``wide_dir`` would do. ``max_cards`` is the wide store's limit (``None``: ``QAR_MAX_CARDS``)."""
    return hub_merge.plan_merge(
        narrow_dir, wide_dir, max_cards=max_cards_from_env() if max_cards is None else max_cards, seed_scope=CARD_SEED_SCOPE,
        vector_dirs=VECTOR_DIRS, limit_name=ENV_MAX_CARDS, vector_store=vector_store, prune_source=prune_source)


def apply_merge(narrow_dir: str, wide_dir: str, *, vector_store: Any = None, prune_source_after: bool = False,
                max_cards: Optional[int] = None) -> MergePlan:
    """Carry out the merge (idempotent). ``max_cards`` is the wide store's limit (``None``: ``QAR_MAX_CARDS``)."""
    return hub_merge.apply_merge(
        narrow_dir, wide_dir, max_cards=max_cards_from_env() if max_cards is None else max_cards, seed_scope=CARD_SEED_SCOPE,
        vector_dirs=VECTOR_DIRS, limit_name=ENV_MAX_CARDS, vector_store=vector_store, prune_source_after=prune_source_after)
