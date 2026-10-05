"""Reserved vector-store scopes shared by the store and the arms that write to it.

One collection holds points of different KINDS: the context CARDS seeded from the keyword store
(``FileContextStore.export_for_embedding``), and the task-to-context ASSOCIATIONS that
``VectorContextAssembler.record`` compounds over time. They are written by different code paths,
they have different lifetimes, and they must not share a capacity bound.

They used to share one: seeding wrote cards UNSCOPED (shared, so every scoped search can see
them), and ``record()`` then bounded "associations" with ``count(scope=None)`` /
``evict_oldest(..., scope=None)``, which is a filter over exactly those unscoped points. So the
first recorded association on a store holding 500+ seeded cards evicted the cards -- every one of
them embedded moments earlier, at real cost, by a seed pass that was about to run again and embed
them all over again.

``CARD_SEED_SCOPE`` fixes that by giving seeded cards a scope of their own. The store treats it as
a VISIBILITY-shared partition: ``_visibility_filter`` admits it under every scope exactly as it
admits unscoped points, so retrieval is unchanged, while ``_exact_scope_filter`` (the one
``count`` and ``evict_oldest`` use) never matches it from any other scope. Cards are therefore
reachable from everywhere and evictable from nowhere.

The value is a plain dict because that is what every ``VectorStore`` method already takes as its
``scope``; the key is namespaced so it cannot collide with a consumer's own scope keys.
"""

from typing import Any, Dict

__all__ = ["CARD_SEED_SCOPE"]


CARD_SEED_SCOPE: Dict[str, Any] = {"qar_point_class": "card_seed"}
