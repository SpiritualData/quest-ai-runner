"""The interface of CardKeeping: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Optional, Tuple
from esoi import solution
from hubofhubs.api import CardStore
from hubofhubs.api import CardRepository


@solution("card-store", kind="store", entry="search", example="rust release")
@typing.runtime_checkable
class CardKeeping(CardStore, typing.Protocol):
    """The hub's ``card-store`` interface (add, get, search, delete, count) over QAR's card persistence: a store the Quest card API or a
    local folder holds, which the arms below present as hub cards."""


@solution("persist-cards", kind="store", entry="read", example="card-1")
@typing.runtime_checkable
class CardPersisting(CardRepository, typing.Protocol):
    """The cards a quest keeps, written and read back whole (the hub's card repository interface)."""


@solution("cache-cards", kind="event", entry="load_listing", example=None)
@typing.runtime_checkable
class CardCache(typing.Protocol):
    """An offline card cache and write outbox (``FileCardCache`` is the durable one).

    A lane keeps working when the backend is unreachable: ``load_listing`` seeds a cold start, ``store_listing`` is called after every successful
    full or delta refresh, and ``enqueue_write`` is offered every write that FAILED so it can be replayed later (``pending_writes``). The
    repository never depends on a cache for correctness: with the default ``NullCardCache`` every method is a no-op and failures stay loud.
    """

    def load_listing(self) -> Optional[Tuple[Dict[str, Dict[str, Any]], float]]:
        """``(cards_by_id, max_updated_at)`` from the last stored listing, or None."""

    def store_listing(self, cards: Dict[str, Dict[str, Any]], max_updated_at: float) -> None:
        """Persist the latest known listing."""

    def enqueue_write(self, card_id: str, card: Dict[str, Any], team_id: str) -> bool:
        """Offer a write that could not reach the backend. Return True if it was queued."""

    def pending_writes(self) -> List[Tuple[str, Dict[str, Any], str]]:
        """Queued ``(card_id, card, team_id)`` writes, oldest first."""
