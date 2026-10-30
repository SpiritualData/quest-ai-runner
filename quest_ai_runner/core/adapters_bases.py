"""ABC bases for implementers who prefer explicit subclassing over a Protocol.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, FrozenSet, List, Optional

from hubofhubs.ports.retrieval import RetrievalAdapterBase  # noqa: F401  (moved to the hub; same class, old import path)

from hubofhubs.ports.scope import ThreadLocalCounter
from .adapters_values import (
    DeepResult,
    Escalation,
    FUTURE_CONTEXT_VIA_OUTPUT,
    Observation,
    ProgressEvent,
    WRITE_SURFACE_AGENT,
)
from hubofhubs.ports.context import AssembledContext
from .adapters_deep import WriteResult
from .adapters_events import Mode


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


class ModelProviderBase(abc.ABC):
    # Thread-local (see ThreadLocalCounter): a provider instance is shared across concurrently
    # running tasks, so these must describe only the calling thread's own usage, not a global
    # total. Every subclass shares these instead of redeclaring its own tokens_in/tokens_out.
    call_count = ThreadLocalCounter()      # Track LLM calls for reporting
    tokens_in = ThreadLocalCounter()
    tokens_out = ThreadLocalCounter()

    def __init__(self):
        self.call_count = 0
        self.tokens_in = 0
        self.tokens_out = 0

    @abc.abstractmethod
    def plan(self, prompt, *, model, tool_schema, layers=None) -> Dict[str, Any]: ...
    @abc.abstractmethod
    def answer(self, messages, *, model, system=None, layers=None) -> str: ...
    @abc.abstractmethod
    def list_models(self) -> List[str]: ...

    # Optional NATIVE web-search capability. Non-abstract so existing providers keep
    # satisfying the ABC; a provider whose SDK ships a web-search tool overrides both.
    def supports_web_search(self, model: Optional[str] = None) -> bool:
        return False

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        raise NotImplementedError("web search not supported by this provider")


class FileWriterBase(abc.ABC):
    """Nominal ABC parallel to the ``FileWriter`` Protocol (same pattern as the other adapters)."""

    @abc.abstractmethod
    def read_file(self, rel_path: str) -> Optional[str]: ...

    @abc.abstractmethod
    def write_file(self, rel_path: str, content: str) -> WriteResult: ...


class OperationWriterBase(abc.ABC):
    """Nominal ABC parallel to the ``OperationWriter`` Protocol (same pattern as the other
    adapters). ``list_writable_operations`` has a concrete default (``[]``, "nothing
    introspectable"), matching ``ModelProviderBase.supports_web_search``'s pattern of a
    non-abstract optional capability; an implementer that CAN describe its own catalog overrides
    it."""

    def list_writable_operations(self) -> List[Dict[str, Any]]:
        return []

    @abc.abstractmethod
    def write_operation(self, name: str, args: Dict[str, Any]) -> WriteResult: ...


class DeepRunnerBase(abc.ABC):
    # How THIS runner returns its future-context bullets (see FUTURE_CONTEXT_VIA_* above).
    #
    #   FUTURE_CONTEXT_VIA_OUTPUT (default) -- the runner's output is PROSE, so the worker is asked
    #       to end it with the delimited FUTURE-CONTEXT section. The orchestrator parses that section
    #       into ``DeepResult.future_context`` and strips it from ``DeepResult.output``.
    #
    #   FUTURE_CONTEXT_VIA_FIELD -- the runner's output is a STRICT FORMAT (generated code, JSON, a
    #       patch) where any appended prose would corrupt the payload. The worker is instead asked to
    #       return its bullets through the runner's own structured channel, and the runner puts them
    #       in ``DeepResult.future_context``. Nothing is ever appended to ``output``, so corruption
    #       is impossible by construction rather than by each consumer remembering to strip.
    #
    # Future context is asked for either way: a code generator knows the most reusable facts of all
    # (entities and ids touched, schema learned, files changed), and the card updater must keep
    # learning from exactly those turns. Read generically with ``getattr(runner,
    # "future_context_channel", FUTURE_CONTEXT_VIA_OUTPUT)``, so duck-typed runners keep working.
    future_context_channel: str = FUTURE_CONTEXT_VIA_OUTPUT

    # What kind of change THIS runner is capable of making (see WRITE_SURFACE_* above). Default is
    # the full agent, so an existing third-party runner that never sets this behaves exactly as
    # today -- only the two bounded rungs (FastEditRunner, MCPOperationRunner) narrow it.
    write_surface: str = WRITE_SURFACE_AGENT

    # Whether THIS runner actually runs the deep-model ladder's ``model`` (see
    # ``runner_uses_deep_model``). True by default, so every existing runner keeps today's
    # behaviour. A runner that answers in-process with its own fixed model (or queues the work for
    # another lane, which picks its own) sets False: the goal loop then never tells a person the
    # work "ran on sonnet" or is "retrying with opus", because neither would be true.
    uses_deep_model: bool = True

    # Whether THIS runner's work OUTLIVES the turn: it queues a task, schedules a job or otherwise
    # starts something that runs and leaves a trace after the reply (see ``runner_starts_background
    # _work``). False by default. The orchestrator never sends its OWN escalations (the verifier's
    # "need more context" and the last-resort deep run before giving up) to such a runner: those
    # runs are the assistant's initiative, and a durable job the person never asked for is a side
    # effect, not a better answer. A planner or user hand-off to the same runner is unaffected.
    starts_background_work: bool = False

    @abc.abstractmethod
    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None,
                 context_preamble=None, run_id=None) -> DeepResult: ...


def runner_starts_background_work(runner: Any) -> bool:
    """Whether ``runner`` starts work that outlives the turn (``starts_background_work``). Read
    with ``getattr(..., False)`` so a duck-typed runner keeps today's behaviour."""
    return bool(getattr(runner, "starts_background_work", False)) if runner is not None else False


def runner_uses_deep_model(runner: Any) -> bool:
    """Whether ``runner`` executes with the deep-model ladder's ``model`` argument.

    Read with ``getattr(..., True)`` so a duck-typed runner that never declares
    ``uses_deep_model`` keeps today's behaviour. ``None`` (no runner wired) is False: nothing runs,
    so no model can be claimed for it."""
    if runner is None:
        return False
    return bool(getattr(runner, "uses_deep_model", True))


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


class ProgressSinkBase(abc.ABC):
    @abc.abstractmethod
    def update(self, event: ProgressEvent, mode: Mode) -> None: ...
