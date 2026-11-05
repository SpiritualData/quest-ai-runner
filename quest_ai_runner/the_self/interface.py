"""ABC bases for implementers who prefer explicit subclassing over a Protocol.

Split out of ``adapters.py`` by definition; that module re-exports every name here. The context-assembler and vector-store
bases (``ContextAssemblerBase``, ``VectorStoreBase``) are ports now: ``hubofhubs.ports.context``.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, FrozenSet, List, Optional

from hubofhubs.api import ModelProviderBase  # noqa: F401  (moved to the hub; same class, old import path)
from hubofhubs.api import RetrievalAdapterBase  # noqa: F401  (moved to the hub; same class, old import path)

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from dataclasses import dataclass, field
from typing import Tuple
import typing
from esoi import solution


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

    def open_decisions_for_quest(self, quest_id: str) -> "List[Dict[str, Any]]":
        """Optional: the open rows themselves (summary, assignee, dedupe key), so the runner can re-file a worker's own ask. Default none."""
        return []

    def withdraw_decision(self, decision_id: str) -> bool:
        """Optional: withdraw one open row (kept as history). Default: cannot, False."""
        return False


@dataclass(frozen=True)
class GoalAnswer:
    """What a turn of the runner returned: the reply text, how the loop ended and what ran."""
    text: str
    kind: str                         # "answer", "deep", "confirm" or "cancelled"
    exit_reason: str = ""
    ran: Tuple[str, ...] = field(default_factory=tuple)    # the goals handed to a deep worker, in order
    question: str = ""                # what the runner asks the person, when kind is "confirm"
    steps: int = 0
    parts: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)    # the flow's parts in order, each with where it ran


@solution("quest-self", kind="event", entry="answer", example="What should I work on first this week?")
@typing.runtime_checkable
class GoalAgent(typing.Protocol):
    """Answers a person's message from their own quests and cards, and does the work they ask for."""

    def answer(self, message: str) -> GoalAnswer: ...
