"""Read-only access to the points of a mounted store that live on ANOTHER Qdrant backend: QAR's binding.

The model (the owner records where its vectors live, ``ReadOnlyVectorReader`` has no write method, compatibility is checked
before any query, an embedded location is never opened) is the hub's: ``hubofhubs.recall.card_vectors``. What stays here is what
the points already stored were written under: the collection prefix ``qar_ctx`` and the seeded-card scope ``CARD_SEED_SCOPE``,
and the wording of the remedy QAR's command line offers for an embedded location.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from hubofhubs.recall import card_vectors as hub
from hubofhubs.recall.card_vectors import (  # noqa: F401  (same objects, old import path)
    CONNECT_TIMEOUT_SECONDS,
    RETRY_AFTER_SECONDS,
    SCOPE_KEY,
    STORE_KEY,
    Compatibility,
    ReaderFactory,
    compatibility,
)

from ..core.vector_scopes import CARD_SEED_SCOPE

DEFAULT_PREFIX = "qar_ctx"


def seed_scope_digest() -> str:
    return hub.seed_scope_digest(CARD_SEED_SCOPE)


def embedded_reason(record: Dict[str, Any]) -> str:
    """Why an ``embedded`` location is not queried; the remedy names QAR's command."""
    return hub.embedded_reason(record, "publish them on a server (`context store-info --vector-location URL`)")


class ReadOnlyVectorReader(hub.ReadOnlyVectorReader):
    """The hub's read-only reader under QAR's collection prefix and seed scope."""

    def __init__(self, client: Any, *, collection_prefix: str = DEFAULT_PREFIX) -> None:
        super().__init__(client, collection_prefix=collection_prefix or DEFAULT_PREFIX, seed_scope=CARD_SEED_SCOPE)


def default_reader_factory(location: str, collection_prefix: str) -> Optional[ReadOnlyVectorReader]:
    """A read-only reader for a server URL. Constructing a client does not connect."""
    return hub.default_reader_factory(location, collection_prefix, CARD_SEED_SCOPE)


class RemoteMountReaders(hub.RemoteMountReaders):
    """The hub's cache of one reader per (location, prefix), with QAR's seed scope."""

    def __init__(self, factory: Optional[ReaderFactory] = None) -> None:
        super().__init__(factory, seed_scope=CARD_SEED_SCOPE)
