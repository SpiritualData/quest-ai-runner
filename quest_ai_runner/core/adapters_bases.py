"""ABC bases for implementers who prefer explicit subclassing over a Protocol.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, FrozenSet, List, Optional

from hubofhubs.ports.model import ModelProviderBase  # noqa: F401  (moved to the hub; same class, old import path)
from hubofhubs.ports.retrieval import RetrievalAdapterBase  # noqa: F401  (moved to the hub; same class, old import path)

from hubofhubs.ports.context import AssembledContext

from .adapters_values import Escalation


class VectorStoreBase(abc.ABC):
    """ABC variant of VectorStore for explicit subclassing.

    Subclasses must implement search / upsert / sync.  All three must never raise
    from the public surface (wrap internals in try/except).

    ``count`` and ``evict_oldest`` are optional capacity-management methods with
    no-op defaults.  Override them in stores that support bounded capacity.
    """

    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        top_k: int = 8,
    ) -> "List[VectorHit]":
        """Embed ``query`` and return top-``top_k`` nearest hits.  Never raises."""

    @abc.abstractmethod
    def upsert(
        self,
        items: "List[Dict[str, Any]]",
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Embed item texts and upsert into the scope collection.  Never raises."""

    @abc.abstractmethod
    def sync(
        self,
        items: "List[Dict[str, Any]]",
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> int:
        """AUTO-UPDATE: re-embed only changed/new items; return count.  Never raises."""

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        """Return the number of stored associations for the given scope.

        No-op default: returns 0.  Override in stores that support capacity
        management.  Never raises.
        """
        return 0

    def evict_oldest(
        self,
        n: int,
        *,
        scope: Optional[Dict[str, Any]] = None,
        ts_key: str = "ts",
    ) -> int:
        """Delete the ``n`` oldest points (sorted by ``ts_key`` payload field, asc).

        No-op default: returns 0.  Override in stores that support capacity
        management.  Never raises.
        """
        return 0


class ContextAssemblerBase(abc.ABC):
    """ABC variant for implementers who prefer explicit subclassing."""

    @abc.abstractmethod
    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> "AssembledContext":
        """Return pre-assembled context for ``task_text``. Never raises."""

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Best-effort write-back -- no-op default; override to persist outcomes."""

    def render_card(
        self, card_id: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """OPTIONAL: render ONE card by id for the mid-loop {"card": id} read. Default: unsupported
        (returns None). Override to fetch a single card's rendered content. Never raises."""
        return None


class GuidanceProviderBase(abc.ABC):
    """ABC variant of GuidanceProvider for implementers who prefer explicit subclassing.

    Subclasses implement ``list`` + ``read`` (both must never raise). ``select`` defaults to
    returning ``[]`` (no pre-selection) — override it to add semantic top-K pre-selection.
    """

    @abc.abstractmethod
    def list(self) -> "List[GuidanceCard]":
        """The catalog (id + title + relevance; body empty). Cheap. Never raises."""

    @abc.abstractmethod
    def read(self, card_id: str) -> "Optional[GuidanceCard]":
        """One card WITH body by id; ``None`` if unknown. Never raises."""

    def select(
        self, user_message: str, *, k: int = 3, meta: Optional[Dict[str, Any]] = None
    ) -> "List[GuidanceCard]":
        """No-op default: returns []. Override to add semantic pre-selection. Never raises."""
        return []


class EscalationSinkBase(abc.ABC):
    @abc.abstractmethod
    def escalate(self, escalation: Escalation) -> str: ...

    def open_decision_ids_for_quest(self, quest_id: str) -> FrozenSet[str]:
        """Best-effort: ids of currently OPEN decisions for ``quest_id``, or empty.

        Concrete (not abstract) so every existing subclass keeps working unchanged. This is an
        OPTIONAL capability a caller can use to recover a decision created OUT OF BAND from this
        sink instance -- e.g. a subprocess deep worker that raised a decision directly against the
        consumer's API using its own credentials and then failed to report the id back (a missing
        or malformed ``QAR-ESCALATED:`` marker) -- by diffing a before/after snapshot instead of
        leaving it permanently orphaned. A sink with no way to look this up returns empty, which a
        caller must read as "nothing new found," never as "confirmed none exist."
        """
        return frozenset()
