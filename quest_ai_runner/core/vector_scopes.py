"""QAR's name for the seeded-card partition of its vector store.

The partition and the seed list type are the hub's (``hubofhubs.recall.vector_scopes``: why seeded cards get a scope of their
own, and how the store treats it). What stays here is the persisted key: a scope's digest is part of every stored point and of
its point id, so the key QAR's existing points were written under (``qar_point_class``) must not change, and QAR hands this
dict to the hub's store and assembler as their ``seed_scope``.
"""
from typing import Any, Dict

from hubofhubs.recall.vector_scopes import SeedItems

__all__ = ["CARD_SEED_SCOPE", "SeedItems"]


CARD_SEED_SCOPE: Dict[str, Any] = {"qar_point_class": "card_seed"}
