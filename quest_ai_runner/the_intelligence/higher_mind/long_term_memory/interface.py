"""The interfaces of RecentContextStore, ConversationRemembering: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Protocol, Union, runtime_checkable
from esoi import solution
from hubofhubs.api import ConversationStore


@solution("long-term-memory", kind="event", entry="load", example="quest-id")
@runtime_checkable
class RecentContextStore(Protocol):
    """Scoped, per-card-and-item persistence of recently selected context. NEVER raises."""

    def record(
        self, scope_keys: Union[str, List[str]], cards: List[Dict[str, Any]], user_text: str
    ) -> None:
        """Persist this turn's selected ``cards`` under EVERY key in ``scope_keys``. Best-effort."""

    def load(self, scope_keys: Union[str, List[str]]) -> List[Dict[str, Any]]:
        """Return recent card records merged across ``scope_keys``, deduped by card id with
        narrower-scope-wins precedence (conv > quest > global)."""


@typing.runtime_checkable
class ConversationRemembering(ConversationStore, typing.Protocol):
    """The hub's conversation-history interface (``current_slice`` of this conversation, ``related_slices`` of the others) over the
    conversations a person already had: a short follow-up is read against them, and no method raises."""
