"""Adapter interfaces — the generic boundary between the brain and any org's specifics.

The orchestrator BRAIN (``core.orchestrator``), the model registry, and the goal-runner
know NOTHING about any specific org, Quest, a database, the filesystem layout, or any user.
They only know these five Protocols. A consumer supplies concrete implementations via
``config.RunnerConfig`` — reference implementations live in ``quest_ai_runner.adapters``.

The five roles:

  * ``RetrievalAdapter`` — GATHER. ``read_section`` / ``grep`` / ``query`` over whatever
    the consumer's source of truth is (indexed files, a live DB, a dev server). This is what
    the brain's plan->read->re-plan loop calls. The brain never opens a file or a socket itself.

  * ``ModelProvider`` — the LLM. ``plan`` (one cheap structured planner decision), ``answer``
    (generate a grounded reply), and ``list_models`` (the live id list the model_registry
    buckets into tiers). Default reference impl wraps the Anthropic SDK.

  * ``DeepRunner`` — spawn a bounded, goal-driven autonomous run (the ``/goal --max-turns``
    contract). The consumer plugs in Claude Code, another agent, or a mock. Returns a
    ``DeepResult`` distinguishing goal-MET from limit/error.

  * ``EscalationSink`` — raise a human-only confirm/decision (the Quest team decision-request
    in production; a recording stub in tests). Returns a decision id the runner reports back.

  * ``ContextAssembler`` — PRE-FLIGHT CONTEXT. Called ONCE before the loop, guaranteed, when
    wired. Assembles task-specific context from a card store / index and feeds it to
    ``run(context_view=...)`` before planning starts. Optional: consumers that omit it get
    exactly today's reactive-gather behaviour.

All five are ``typing.Protocol`` so a consumer can satisfy them structurally (duck typing) —
no inheritance required — but ABCs (``RetrievalAdapterBase`` etc.) are also offered for
implementers who prefer explicit subclassing. Keeping these tiny and dependency-free is the
whole point: a stranger's org can adopt the library by implementing five small surfaces.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# The TWO PRODUCT MODES (one brain). The orchestrator emits the SAME events in
# both; the Mode only selects which ProgressSink the consumer wires, and the
# sink — never the orchestrator — decides what actually surfaces.
# ---------------------------------------------------------------------------

class Mode(enum.Enum):
    """Which product lane a run executes in.

    * ``LIVE``       — a human is attending. Every event streams to the consumer in real time
                       (status ticks, the reply, confirm requests). Used in-process by chat.
    * ``BACKGROUND`` — sent-off / scheduled; nobody is watching. Surfaces NOTHING intermediate —
                       only a result, a decision-request, or an explicit milestone. The runner's
                       poll-by-due lane uses this.
    """
    LIVE = "live"
    BACKGROUND = "background"


# Event types the orchestrator emits through the ProgressSink. Stable strings so consumers
# (quest-backend, the cockpit) can switch on them. "Chatter" types (status/plan/read/replan)
# are the ones a BACKGROUND sink drops; the rest always surface.
EVENT_STATUS = "status"        # a human-facing progress tick ("reading…", "answering")
EVENT_PLAN = "plan"            # the planner chose a next step (planning/re-planning chatter)
EVENT_READ = "read"            # a gather step ran (reading/grepping chatter)
EVENT_REPLAN = "replan"        # the loop is re-planning with what it just gathered (chatter)
EVENT_PARTIAL = "partial"      # a partial/streaming chunk of the reply (LIVE only)
EVENT_EXEC = "exec"            # a deep-run EXECUTION-lifecycle tick — generated code, an
                               # execution attempt, its raw output, a retry, done/error.
                               # Carries structured ``data`` (phase/code/attempt/output/error).
                               # LIVE-only texture: like the chatter types it is NOT in
                               # SURFACING_EVENTS, so a BACKGROUND run drops it (the runner
                               # still posts its own milestones/result); a LIVE run shows it.
EVENT_RESULT = "result"        # the final answer / deep output (ALWAYS surfaces)
EVENT_DECISION = "decision"    # a confirm / human decision-request was raised (ALWAYS surfaces)
EVENT_MILESTONE = "milestone"  # an explicit, real milestone worth surfacing (ALWAYS surfaces)
EVENT_DONE = "done"            # the run reached a terminal state (ALWAYS surfaces)

# The event types a BACKGROUND (MilestoneSink) run forwards. Everything else is dropped as
# intermediate chatter. Encoded ONCE here so every consumer inherits the same policy.
SURFACING_EVENTS = frozenset({EVENT_RESULT, EVENT_DECISION, EVENT_MILESTONE, EVENT_DONE})


# ---------------------------------------------------------------------------
# Value objects passed across the boundary (plain data, no behavior).
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """One thing that happened during a run, emitted by the orchestrator to a ProgressSink.

    The orchestrator emits these as it works; it does NOT decide what is shown — the sink
    (chosen by Mode) does. ``type`` is one of the ``EVENT_*`` constants above.
    """
    type: str
    text: Optional[str] = None             # human-facing message (status tick, reply, milestone)
    step: Optional[int] = None             # loop step index, when relevant
    action: Optional[str] = None           # the planner action, for plan/replan events
    decision_id: Optional[str] = None      # for decision events
    result_kind: Optional[str] = None      # for result/done events: "answer" | "deep" | "confirm"
    data: Dict[str, Any] = field(default_factory=dict)  # any extra structured payload

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type}
        for k in ("text", "step", "action", "decision_id", "result_kind"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.data:
            d["data"] = self.data
        return d




@dataclass
class Observation:
    """One result of a gather step — a read, a grep, or a structured query.

    ``kind`` is "read" | "grep" | "query" | "error". The brain renders these into the
    planner prompt, so they stay small and JSON-serializable.
    """
    kind: str
    rel_path: Optional[str] = None
    locator: Optional[str] = None         # e.g. "lines 10-40", "heading='Metrics'"
    text: Optional[str] = None            # for read/query
    pattern: Optional[str] = None         # for grep
    scope: Optional[str] = None
    hits: List[Dict[str, Any]] = field(default_factory=list)  # for grep
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"kind": self.kind}
        for k in ("rel_path", "locator", "text", "pattern", "scope", "error"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.hits:
            d["hits"] = self.hits
        return d


@dataclass
class PlanDecision:
    """The planner's structured next-step decision (one iteration of the loop)."""
    action: str                                   # "read" | "answer" | "deep" | "confirm"
    reads: List[Dict[str, Any]] = field(default_factory=list)
    goal: Optional[str] = None
    deep_brief: Optional[str] = None
    confirm_question: Optional[str] = None
    model_tier: Optional[str] = None              # "haiku" | "sonnet" | "opus" | None
    subquestions: List[str] = field(default_factory=list)
    deep_subtasks: List[Dict[str, Any]] = field(default_factory=list)
    rationale: str = ""


@dataclass
class DeepResult:
    """The outcome of a goal-driven deep run."""
    met: bool                 # True = the run met the written goal cleanly
    output: str = ""
    error: Optional[str] = None
    decision_id: Optional[str] = None   # set if the run needed a human decision instead


@dataclass
class Escalation:
    """A human-only step the brain refuses to take autonomously."""
    summary: str
    kind: str = "approve"
    quest_id: Optional[str] = None
    assignee: Optional[str] = None              # consumer-defined routing key
    default_on_silence: str = "hold"


# ---------------------------------------------------------------------------
# The four adapter Protocols (structural) + parallel ABCs (nominal).
# ---------------------------------------------------------------------------

@runtime_checkable
class RetrievalAdapter(Protocol):
    """GATHER source. The brain calls these to ground before answering."""

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Return a SPECIFIC PART of a source item (a line range, a heading section, or
        the head). Must never raise — return an Observation(kind="error", ...) instead."""

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """LOCATE a regex across the source, returning hit lines. Never raises."""

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Optional structured lookup (e.g. a cached DB read). A RetrievalAdapter that
        only does files may return Observation(kind="error", error="query unsupported")."""

    # --- DISCOVERY: let the brain learn what exists before it reads or acts -----------
    # These four make the source self-describing so the brain never needs a static schema/
    # operation blob pushed into its prompt — it asks. All return Observation(kind="query")
    # so they flow into ``gathered`` through the SAME path as a read, and never raise.
    # Two levels each: a cheap LIST (names + one-liners) and a DESCRIBE drill-down.

    def list_sources(self) -> Observation:
        """DISCOVERY (cheap): the readable sources that exist — collections, tables, doc-sets —
        one short line each (name + what it holds). The brain calls this when the context does
        not already name the source it needs. Never raises."""

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """DISCOVERY (drill-down): the fields/types of ONE source named by ``list_sources``.
        ``path`` optionally drills into a nested field/sub-document for multi-level detail.
        Never raises."""

    def list_operations(self) -> Observation:
        """DISCOVERY (cheap): the operations the consumer makes callable — both reads
        (e.g. "get latest insights") and mutations — one short line each (name + effect).
        The brain calls this before authoring a change, so it acts via a real operation
        instead of free-associating a shape. Never raises."""

    def describe_operation(self, name: str) -> Observation:
        """DISCOVERY (drill-down): the full signature/usage/example for ONE operation named
        by ``list_operations``. Never raises."""


@runtime_checkable
class ModelProvider(Protocol):
    """The LLM behind planning, answering, and the live model list."""

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        """One cheap, forced-structured planner call -> the raw decision dict."""

    def answer(self, messages: List[Dict[str, str]], *, model: str, system: Optional[str] = None) -> str:
        """Generate a grounded reply from a message list. Returns the text."""

    def list_models(self) -> List[str]:
        """The live, latest-first model id list (model_registry buckets it into tiers)."""


@runtime_checkable
class DeepRunner(Protocol):
    """Spawn a bounded, goal-driven autonomous run (the ``/goal --max-turns`` contract)."""

    def run_goal(
        self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
        emit: Optional[Callable[[ProgressEvent], None]] = None,
    ) -> DeepResult:
        """Run an autonomous worker toward ``goal`` (a written done-standard), bounded by
        ``max_turns``. Return a DeepResult distinguishing met-vs-limit. Never raises.

        ``emit`` (optional) lets a long-running deep runner report its EXECUTION LIFECYCLE as it
        works — generated code, each execution attempt, its raw output, retries, done — by emitting
        ``ProgressEvent(type=EVENT_EXEC, ...)``. The orchestrator routes these through the run's
        sink, so they show live (LIVE) and are dropped as chatter (BACKGROUND), exactly like other
        intermediate texture. Runners that don't stream may ignore it. The orchestrator only passes
        ``emit`` to runners whose ``run_goal`` accepts it, so older signatures keep working."""


@runtime_checkable
class EscalationSink(Protocol):
    """Raise a human-only confirm/decision and return its id."""

    def escalate(self, escalation: Escalation) -> str:
        """Create a decision-request (or equivalent) and return its decision id."""


@runtime_checkable
class ProgressSink(Protocol):
    """The "inform along the way" discipline — the ONE place messaging policy lives.

    The orchestrator routes EVERY internal event through ``update(event, mode)``. The sink
    decides what (if anything) surfaces to the consumer. The orchestrator never decides
    messaging policy itself; it only emits. Mode is passed so a single sink could vary its
    behavior, but the two reference sinks below are mode-shaped by construction:

      * ``StreamSink``    — LIVE: forwards every event to the consumer's stream.
      * ``MilestoneSink`` — BACKGROUND: drops planning/reading/re-planning chatter; forwards
                            only ``result`` / ``decision`` / ``milestone`` / ``done``.
    """

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        """Receive one event. Decide whether/how it surfaces. Must never raise."""


# ---------------------------------------------------------------------------
# The fifth adapter role: ContextAssembler (PRE-FLIGHT CONTEXT).
# ---------------------------------------------------------------------------

@dataclass
class AssembledContext:
    """The pre-assembled context produced by a ContextAssembler before the loop starts.

    Guaranteed injection: when a ContextAssembler is wired, the Orchestrator feeds
    ``context_view`` and (optionally) ``model_tier_hint`` into ``run()`` automatically.

    Fields:
      ``context_view``      -- pre-assembled context string; fed to run(context_view=...).
      ``model_tier_hint``   -- "haiku" | "sonnet" | "opus" | None; overrides the per-run
                               default when the caller passed no explicit model_hint.
      ``card_ids``          -- the card ids that fed this view (for tracing / tests).
      ``stale``             -- cards or file paths found stale during assembly (re-derived).
    """
    context_view: str = ""
    model_tier_hint: Optional[str] = None
    card_ids: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)


@runtime_checkable
class ContextAssembler(Protocol):
    """PRE-FLIGHT CONTEXT. Called ONCE before the loop, guaranteed, if wired.

    ``assemble`` selects and renders task-relevant context (e.g. from a card store or a
    vector index) into a string the Orchestrator feeds as ``context_view``. It NEVER raises
    -- return an empty AssembledContext() on any failure. ``record`` is a best-effort
    write-back after the run; it NEVER raises either.
    """

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> "AssembledContext":
        """Return pre-assembled context for ``task_text``. Never raises."""

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Best-effort write-back of the run outcome. Never raises."""


# --- ABC variants for implementers who prefer explicit subclassing -----------

import abc


class ContextAssemblerBase(abc.ABC):
    """ABC variant for implementers who prefer explicit subclassing."""

    @abc.abstractmethod
    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> "AssembledContext":
        """Return pre-assembled context for ``task_text``. Never raises."""

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Best-effort write-back -- no-op default; override to persist outcomes."""


class RetrievalAdapterBase(abc.ABC):
    @abc.abstractmethod
    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None, max_bytes=None) -> Observation: ...
    @abc.abstractmethod
    def grep(self, pattern, *, scope=None, max_hits=None) -> Observation: ...

    def query(self, spec: Dict[str, Any]) -> Observation:  # optional default
        return Observation(kind="error", error="query not supported by this adapter")

    # Discovery defaults — non-abstract so existing adapters keep satisfying the ABC. An
    # adapter that can enumerate its sources/operations overrides these; one that can't
    # returns a benign "nothing to discover" Observation (never an error that stalls the loop).
    def list_sources(self) -> Observation:
        return Observation(kind="query", locator="list_sources",
                           text="No sources are enumerable for this adapter.")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        return Observation(kind="query", locator=f"describe_source({name})",
                           text=f"No schema available for source {name!r}.")

    def list_operations(self) -> Observation:
        return Observation(kind="query", locator="list_operations",
                           text="No callable operations are advertised by this adapter.")

    def describe_operation(self, name: str) -> Observation:
        return Observation(kind="query", locator=f"describe_operation({name})",
                           text=f"No detail available for operation {name!r}.")


class ModelProviderBase(abc.ABC):
    @abc.abstractmethod
    def plan(self, prompt, *, model, tool_schema) -> Dict[str, Any]: ...
    @abc.abstractmethod
    def answer(self, messages, *, model, system=None) -> str: ...
    @abc.abstractmethod
    def list_models(self) -> List[str]: ...


class DeepRunnerBase(abc.ABC):
    @abc.abstractmethod
    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None) -> DeepResult: ...


class EscalationSinkBase(abc.ABC):
    @abc.abstractmethod
    def escalate(self, escalation: Escalation) -> str: ...


class ProgressSinkBase(abc.ABC):
    @abc.abstractmethod
    def update(self, event: ProgressEvent, mode: Mode) -> None: ...


# ---------------------------------------------------------------------------
# The two reference ProgressSinks. The surfacing RULE lives HERE (once), so every
# consumer inherits identical messaging policy regardless of how they wire things.
# ---------------------------------------------------------------------------

class StreamSink(ProgressSinkBase):
    """LIVE sink — forward EVERYTHING to the consumer's stream in real time.

    Construct with a ``forward`` callable (e.g. a websocket/SSE push, or appending to a list
    in tests). It is called with each event's ``dict`` form. A failing forward never breaks
    the run — the orchestrator's work continues even if the stream drops.
    """

    def __init__(self, forward: Callable[[Dict[str, Any]], None]):
        self._forward = forward

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        try:
            self._forward(event.to_dict())
        except Exception:  # noqa: BLE001 — a dropped stream must not break the run
            pass


class MilestoneSink(ProgressSinkBase):
    """BACKGROUND sink — surface ONLY result / decision / milestone / done.

    Planning, reading, re-planning, status ticks, and partial chunks are dropped: nobody is
    attending, so intermediate chatter is noise. The same surfacing set (``SURFACING_EVENTS``)
    is enforced here once. Construct with optional callables for each surfacing kind; the
    runner's executor wires these to (optional progress note / Quest decision-request / final
    PATCH). Any unset callable means "that kind is simply not forwarded anywhere."
    """

    def __init__(
        self,
        *,
        on_milestone: Optional[Callable[[ProgressEvent], None]] = None,
        on_decision: Optional[Callable[[ProgressEvent], None]] = None,
        on_result: Optional[Callable[[ProgressEvent], None]] = None,
        on_done: Optional[Callable[[ProgressEvent], None]] = None,
    ):
        self._on_milestone = on_milestone
        self._on_decision = on_decision
        self._on_result = on_result
        self._on_done = on_done

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        if event.type not in SURFACING_EVENTS:
            return  # drop planning/reading/re-planning/status/partial chatter
        try:
            if event.type == EVENT_MILESTONE and self._on_milestone:
                self._on_milestone(event)
            elif event.type == EVENT_DECISION and self._on_decision:
                self._on_decision(event)
            elif event.type == EVENT_RESULT and self._on_result:
                self._on_result(event)
            elif event.type == EVENT_DONE and self._on_done:
                self._on_done(event)
        except Exception:  # noqa: BLE001 — a sink callback must not break the run
            pass


class FanoutSink(ProgressSinkBase):
    """Route events to one of two sinks, switchable mid-run (the LIVE↔BACKGROUND handoff).

    Starts forwarding to ``live`` (the attended stream). If the consumer detaches, call
    ``detach()`` to switch to ``background`` (a MilestoneSink) for the REST of the run — the
    work continues to completion and its result/decision/done is delivered via the background
    path instead of the dropped stream. Idempotent: once detached, stays detached.
    """

    def __init__(self, live: ProgressSink, background: ProgressSink):
        self._live = live
        self._background = background
        self._detached = False

    @property
    def detached(self) -> bool:
        return self._detached

    def detach(self) -> None:
        """Switch all subsequent events to the background sink."""
        self._detached = True

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        sink = self._background if self._detached else self._live
        # When detached, surface as BACKGROUND so the milestone policy applies to what's left.
        eff_mode = Mode.BACKGROUND if self._detached else mode
        try:
            sink.update(event, eff_mode)
        except Exception:  # noqa: BLE001
            pass
