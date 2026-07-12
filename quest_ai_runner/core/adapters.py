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

  * ``VectorStore`` — OPTIONAL VECTOR ORIENTATION. Embeds items and queries, upserts points
    into a collection scoped by org/team/quest, and retrieves semantically similar candidates.
    Complements keyword/IDF (``FileContextStore``) rather than replacing it: keyword catches
    exact identifiers; vectors catch semantics. The ``sync`` method is the AUTO-UPDATE entry
    point: it re-embeds only items whose fingerprint changed, so the index stays fresh with
    zero user management. Heavy deps (qdrant-client, fastembed) are optional; the Protocol is
    defined here in stdlib so the core stays dependency-free.

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
EVENT_UNDERSTANDING = "understanding"  # Step 1 produced a goal condition (the resolved request).
                               # Fired only when User Input Understanding actually ran (a short/
                               # anaphoric input was resolved against conversation context). Carries
                               # ``data`` with the goal_condition. ALWAYS surfaces (in SURFACING_EVENTS).
EVENT_CONTEXT = "context"      # context assembled for this turn: cards selected + sources.
                               # Fired when a ContextAssembler is wired and produces card_metadata.
                               # Carries ``data`` with card_metadata list and sources (ALWAYS surfaces).
EVENT_RESULT = "result"        # the final answer / deep output (ALWAYS surfaces)
EVENT_DECISION = "decision"    # a confirm / human decision-request was raised (ALWAYS surfaces)
EVENT_MILESTONE = "milestone"  # an explicit, real milestone worth surfacing (ALWAYS surfaces)
EVENT_DONE = "done"            # the run reached a terminal state (ALWAYS surfaces)
EVENT_TOKENS = "tokens"        # cumulative token counts after a model call (ALWAYS surfaces)
EVENT_OVERSEER = "overseer"    # a minimal-intervention OVERSEER consultation happened this run —
                               # its signal (proceed/redirect/answer_now/escalate) + optional hint.
                               # ALWAYS surfaces (in SURFACING_EVENTS) so a BACKGROUND run can note a
                               # course correction; it fires on every consultation, including proceed.

# The event types a BACKGROUND (MilestoneSink) run forwards. Everything else is dropped as
# intermediate chatter. Encoded ONCE here so every consumer inherits the same policy.
SURFACING_EVENTS = frozenset({EVENT_UNDERSTANDING, EVENT_CONTEXT, EVENT_RESULT, EVENT_DECISION, EVENT_MILESTONE, EVENT_DONE, EVENT_TOKENS, EVENT_OVERSEER})


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
    action: str                                   # "read" | "answer" | "deep" | "confirm" | "clarify"
    reads: List[Dict[str, Any]] = field(default_factory=list)
    goal: Optional[str] = None
    deep_brief: Optional[str] = None
    confirm_question: Optional[str] = None
    model_tier: Optional[str] = None              # "haiku" | "sonnet" | "opus" | None
    subquestions: List[str] = field(default_factory=list)
    deep_subtasks: List[Dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    # Optional deferred deep work: when action="answer", can also queue a deep task to run after
    deferred_deep: Optional[Dict[str, Any]] = None  # {"goal": "...", "brief": "...", "rationale": "..."}
    # Explicit signal: this answer contains work that needs execution (instead of regex guessing)
    answer_contains_work_to_execute: bool = False  # Set True if answer describes work the AI should do
    # User clarification/input needed: when action="clarify"
    clarification: Optional[Dict[str, Any]] = None  # {"question": "...", "options": [...], "allow_free_input": bool}


@dataclass
class DeepResult:
    """The outcome of a goal-driven deep run."""
    met: bool                 # True = the run met the written goal cleanly
    output: str = ""
    error: Optional[str] = None
    decision_id: Optional[str] = None   # set if the run needed a human decision instead
    # ASYNC HAND-OFF marker. True = this runner did NOT execute the goal inline; it queued the real
    # run to finish out-of-band and report its outcome back later (e.g. a chat deep runner that
    # creates a tracked task and returns a "task #N launched" sentinel). When set, the goal loop
    # trusts ``met`` and STOPS — it must not re-verify the sentinel ``output`` against the goal
    # (that always fails) nor relaunch, which would spawn a fresh task every iteration. The real
    # result is verified when it reflects back, not here. Inline runners leave this False.
    deferred: bool = False
    # Resource usage the worker reported for THIS run, when available (0 / 0.0 if the runner does
    # not report it). The goal loop accumulates these to enforce an overall token budget instead of
    # a fixed attempt count.
    tokens: int = 0           # input + output tokens consumed by the worker for this run
    cost_usd: float = 0.0     # worker-reported cost for this run, when available


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

    def supports_web_search(self, model: Optional[str] = None) -> bool:
        """Whether this provider can search the live web natively (no extra API key).

        Optional capability. Providers that wrap an SDK with a built-in web-search tool
        (Anthropic's ``web_search`` server tool, Gemini's Google Search grounding) return
        True when their LLM key is configured; others return False.
        """

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        """Search the live web via the provider's NATIVE tool and return a result dict.

        Returns ``{"answer": <synthesized text>, "results": [{"title","url","snippet"}, ...]}``.
        Uses the same LLM API key the provider already has (no separate web-search key).
        Callers should gate on ``supports_web_search(model)`` first.
        """


@runtime_checkable
class DeepRunner(Protocol):
    """Spawn a bounded, goal-driven autonomous run (the ``/goal --max-turns`` contract)."""

    def run_goal(
        self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None,
        emit: Optional[Callable[[ProgressEvent], None]] = None,
        context_preamble: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> DeepResult:
        """Run an autonomous worker toward ``goal`` (a written done-standard), bounded by
        ``max_turns``. Return a DeepResult distinguishing met-vs-limit. Never raises.

        ``emit`` (optional) lets a long-running deep runner report its EXECUTION LIFECYCLE as it
        works — generated code, each execution attempt, its raw output, retries, done — by emitting
        ``ProgressEvent(type=EVENT_EXEC, ...)``. The orchestrator routes these through the run's
        sink, so they show live (LIVE) and are dropped as chatter (BACKGROUND), exactly like other
        intermediate texture. Runners that don't stream may ignore it. The orchestrator only passes
        ``emit`` to runners whose ``run_goal`` accepts it, so older signatures keep working.

        ``context_preamble`` (optional) is a PER-CALL context preamble for THIS run only. The
        orchestrator forwards it (when the caller supplies one, e.g. an AI rep's pulled persona)
        so the run executes with that context without mutating any shared runner state. A runner
        that accepts it should prepend it ahead of its own configured preamble (or use it in
        place of one). Like ``emit``, it is passed ONLY to runners whose ``run_goal`` accepts it,
        so older signatures keep working and callers that pass nothing see prior behaviour.

        ``run_id`` (optional) is the STABLE id for this subgoal's whole retry sequence (the
        orchestrator generates it once per subgoal, before its first attempt). A runner that
        streams ``emit`` events should tag every event from every retry with this SAME id, since a
        retry may spawn an entirely new underlying process/session — without a stable id, a
        consumer's dashboard would otherwise show each retry as a separate duplicate run for what
        is really one ongoing subgoal. Passed ONLY to runners whose ``run_goal`` accepts it."""


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
      ``sources``           -- OPTIONAL context-transparency list.  Each entry describes one
                               contributing arm, e.g.::

                                   {
                                     "adapter": "keyword",       # "keyword|vector|bm25|task_memory|hybrid"
                                     "label":   "docstring cards",
                                     "items":   ["path/or/id", ...],
                                   }

                               Defaults to [] (backward compatible).  Populated by assemblers
                               that opt in; the Orchestrator emits a human-readable STATUS
                               event summarising the sources when the list is non-empty.
    """
    context_view: str = ""
    model_tier_hint: Optional[str] = None
    card_ids: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    card_metadata: List[Dict[str, Any]] = field(default_factory=list)  # [{'id', 'title', 'relevance_score', 'file_count', 'files', 'adapter'}]


# ---------------------------------------------------------------------------
# Conversation-history retrieval (storage-agnostic): the User Input Understanding
# step uses this to resolve a short/anaphoric message into a self-contained goal
# condition. A local reference impl reads Claude session files; a different backend
# (Mongo, etc.) can satisfy the same Protocol.
# ---------------------------------------------------------------------------

@dataclass
class ConversationContext:
    """A rendered slice of conversation history, ready to drop into ``context_view``.

    Fields:
      ``text``          -- rendered, ready-to-inject text (role-labelled turns).
      ``turns``         -- optional metadata of the turns included (for tracing / tests).
      ``sources``       -- optional ``[{conv_id, label, ...}]`` describing which conversations fed it.
      ``scanned``       -- how many turns/conversations were considered (transparency).
      ``truncated``     -- True if some content was dropped to respect a ``max_chars`` budget.
      ``degraded_note`` -- set when ``filters`` were given but matched nothing, so the store fell
                           back to relevance-only (today's behavior) instead of an empty result. The
                           SAME note is also prepended into ``text`` (a labeled line) so it reaches
                           the prompt; this field mirrors it for tracing/tests. None when filters
                           were absent, or present and satisfied.
    """
    text: str = ""
    turns: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    degraded_note: Optional[str] = None


@runtime_checkable
class ConversationStore(Protocol):
    """Storage-agnostic conversation-history retrieval. NEVER raises (return empty on failure).

    The brain's User Input Understanding step calls these to resolve a short/anaphoric message
    (``"ok do it"``, ``"the first one"``) into a self-contained goal condition. The reference
    impl (``adapters.SessionFileConversationStore``) reads local Claude session files; a host can
    implement the same Protocol over Mongo or any other backend.
    """

    def current_slice(self, conv_id: str, query: str, *, recent_turns: int = 4,
                      max_chars: int = 6000,
                      filters: Optional[Dict[str, Any]] = None) -> "ConversationContext":
        """Relevant slice of the CURRENT conversation. The ONLY thing forced into the output is the
        LAST USER turn; everything else is a relevance-selected CANDIDATE (TF-DF-IDF), not
        auto-included by recency. ``recent_turns`` is the "considered window" (default 4): the last N
        turns join the candidate pool but are NOT guaranteed in. USER turns are preferred and rendered
        verbatim; AI turns earn inclusion by relevance and are compacted. Scalable for very long
        conversations. ``filters`` (optional) is an OPAQUE dict of retrieval constraints -- keys are
        interpreted by the implementation (e.g. ``time_range``, ``topic_terms``, ``actor``,
        ``content_kind``; a consumer may add its own domain keys, e.g. an id list, since core never
        reads them). An implementation that has no meaningful use for ``filters`` at this single-
        conversation granularity may ignore it. Never raises."""

    def related_slices(self, query: str, scope: Dict[str, Any], *, exclude_conv_id: Optional[str] = None,
                       max_convs: int = 3, max_chars: int = 6000,
                       filters: Optional[Dict[str, Any]] = None) -> "ConversationContext":
        """TF-DF-IDF-selected slices from OTHER conversations within ``scope``
        ({user_id, team_ids, since, participant_id} — interpreted by the impl). ``filters``
        (optional) is an OPAQUE dict of retrieval constraints applied as a HARD filter BEFORE
        relevance ranking (e.g. ``time_range`` over each candidate's timestamp) -- relevance then
        ranks WITHIN the filtered candidates. When the filtered set is empty, an implementation
        SHOULD degrade to today's relevance-only behavior over the unfiltered candidates rather than
        return nothing, and set ``ConversationContext.degraded_note`` (plus a labeled line in
        ``text``) so the caller can tell the difference. Keys the implementation does not recognize
        are ignored. Never raises."""


@runtime_checkable
class ContextAssembler(Protocol):
    """PRE-FLIGHT CONTEXT. Called ONCE before the loop, guaranteed, if wired.

    ``assemble`` selects and renders task-relevant context (e.g. from a card store or a
    vector index) into a string the Orchestrator feeds as ``context_view``. It NEVER raises
    -- return an empty AssembledContext() on any failure. ``record`` is a best-effort
    write-back after the run; it NEVER raises either.
    """

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, on_event: Optional[Any] = None
    ) -> "AssembledContext":
        """Return pre-assembled context for ``task_text``. Never raises.

        ``on_event``, if given, is a callable ``(event_type: str, data: dict) -> None`` that
        receives progress events as assembly progresses. Callers that do not care about events
        may omit it; implementations that do not support it may ignore it.

        ``meta`` MAY carry ``"recent_item_usage"`` -- ``{card_id: [item_id, ...]}``, an optional
        HINT built by ``core.recent_context.build_item_usage_hint`` from the warm recent-context
        store's memory of which items past turns found useful for a similar input. An assembler
        that does not know this key MUST simply ignore it (it is purely additive); the reference
        ``HybridContextAssembler`` threads it into its consolidating LLM pass so previously-useful
        items are preferred/ordered first, never hard-enforced.
        """

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Best-effort write-back of the run outcome. Never raises."""

    # OPTIONAL capability: render ONE card by id (the mid-loop {"card": id} read).
    # The unified context primitive (see docs/HANDS_FREE_QUEST_AI_DESIGN.md sec. 3) makes card
    # access reachable at EVERY loop step, not just turn start. The Orchestrator dispatches this via
    # getattr, so an assembler that omits it degrades to a benign "not supported" observation -- it
    # is NOT part of the required surface. Best-effort: return the card's rendered content (its
    # references resolved) as text, or None when the card is absent or the store cannot render it.
    # Never raises.
    #
    #   def render_card(
    #       self, card_id: str, *, meta: Optional[Dict[str, Any]] = None
    #   ) -> Optional[str]:
    #       ...


# ---------------------------------------------------------------------------
# VectorStore: optional VECTOR ORIENTATION role (Protocol + ABC, stdlib-only).
# Heavy deps (qdrant-client, fastembed) live in adapters.qdrant_vector_store
# behind the [qdrant] optional extra.  This Protocol is stdlib so the core
# stays dependency-free.
# ---------------------------------------------------------------------------

@dataclass
class VectorHit:
    """One result returned by a VectorStore search.

    Fields:
      ``id``      -- the item id as given to ``upsert``/``sync``.
      ``score``   -- similarity score (higher is more similar; range depends on metric).
      ``text``    -- the item text (may be empty if the store did not return it).
      ``payload`` -- arbitrary metadata attached at upsert time.
    """
    id: str
    score: float
    text: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """OPTIONAL VECTOR ORIENTATION.  Semantic search + auto-updating index.

    Implementations embed items internally (they manage the embedder); callers
    pass raw text.  All methods NEVER raise — return [] / 0 on any error so that
    a missing or degraded vector store falls back gracefully to keyword search.

    ``search`` — embed ``query`` and retrieve up to ``top_k`` nearest neighbours
                 from the scope-derived collection.
    ``upsert`` — embed item texts and upsert points keyed by item id, storing
                 payload + fingerprint.  Each item dict: {id, text, payload?, fingerprint?}.
    ``sync``   — AUTO-UPDATE: fetch stored fingerprints, re-embed+upsert ONLY items
                 whose ``fingerprint`` differs from what is stored or are missing.
                 Returns the count of items re-embedded.  This is the zero-management
                 auto-update entry point: changed items are lazily re-indexed on use.

    ``scope``  — a dict (e.g. {org_id, team_id, quest_id}) that selects which
                 collection to operate on.  None means the default collection.

    OPTIONAL CAPACITY METHODS (count / evict_oldest)
    -------------------------------------------------
    Stores that support capacity management may implement:

    ``count``         — return the number of stored associations (for the given
                        scope); return 0 if unsupported.
    ``evict_oldest``  — delete the ``n`` oldest points (sorted by the ``ts_key``
                        payload field, ascending); return the number actually
                        deleted.  Return 0 if unsupported or on any error.

    These are NOT part of the structural ``VectorStore`` Protocol check (to keep
    backward compat) — callers detect them via ``hasattr``.  The ABC below
    provides no-op defaults so existing subclasses keep working.
    """

    def search(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        top_k: int = 8,
    ) -> "List[VectorHit]":
        """Embed ``query`` and return the top-``top_k`` nearest hits.  Never raises."""

    def upsert(
        self,
        items: "List[Dict[str, Any]]",
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Embed item texts and upsert into the scope collection.  Never raises."""

    def sync(
        self,
        items: "List[Dict[str, Any]]",
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> int:
        """AUTO-UPDATE: re-embed only changed/new items; return count re-embedded.  Never raises."""


# --- ABC variants for implementers who prefer explicit subclassing -----------

import abc


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


# ---------------------------------------------------------------------------
# An OPTIONAL adapter role: GuidanceProvider (USE-CASE-SPECIFIC INSTRUCTIONS).
# ---------------------------------------------------------------------------
#
# Lets a host app keep its ALWAYS-ON core prompt small by moving instructions that
# apply to only SOME inputs (product facts, feature-flow guides, behavior policies) out of
# the static prompt and into a retrievable corpus of "guidance cards". The brain treats each
# card as OPAQUE TEXT — it knows nothing of what a card says, only that the host app supplies
# them. When a GuidanceProvider is wired, the orchestrator can pre-SELECT the cards most
# relevant to the user's message before planning, and the planner can also LIST the catalog
# and READ a specific card on demand, through the SAME observation machinery as a read.
#
# Purely additive: a consumer that supplies no GuidanceProvider sees exactly today's behavior.

@dataclass
class GuidanceCard:
    """One use-case-specific instruction unit, OPAQUE to the brain.

    Fields:
      ``id``        -- stable identifier the planner uses to ``read`` the card.
      ``title``     -- short human-facing name (shown in the catalog).
      ``relevance`` -- a one-line "when does this apply?" hint used for retrieval / pre-selection
                       (the catalog carries id + title + relevance, NOT the body, so it stays cheap).
      ``body``      -- the full instruction text. Empty in catalog (``list``) results; populated
                       by ``read`` / ``select``. The brain never interprets it — it just renders it.
      ``tags``      -- optional scope/operation tags used by UniversalGuidanceProvider for
                       hierarchical selection (e.g. "scope:global", "operation:plan").
    """
    id: str
    title: str
    relevance: str
    body: str = ""
    tags: List[str] = field(default_factory=list)


@runtime_checkable
class GuidanceProvider(Protocol):
    """OPTIONAL USE-CASE-SPECIFIC INSTRUCTIONS. The host app supplies retrievable guidance.

    All three methods NEVER raise — return ``[]`` / ``None`` on any error so a missing or
    degraded provider falls back gracefully (the run proceeds with no guidance).

    ``list``   — the CATALOG: every card's id + title + relevance, body EMPTY. Cheap; the brain
                 calls it (via ``list_guidance``) to discover what guidance exists.
    ``read``   — ONE card WITH body, by id; ``None`` if the id is unknown. The brain calls it
                 (via ``read_guidance``) when it needs the full instruction text.
    ``select`` — OPTIONAL semantic PRE-SELECTION: the top-``k`` cards (WITH bodies) most relevant
                 to ``user_message``. The orchestrator calls it ONCE before planning to inject an
                 "APPLICABLE GUIDANCE" block. May return ``[]`` (no opinion / not implemented).
    """

    def list(self) -> "List[GuidanceCard]":
        """The catalog (id + title + relevance; body empty). Cheap. Never raises."""

    def read(self, card_id: str) -> "Optional[GuidanceCard]":
        """One card WITH body by id; ``None`` if unknown. Never raises."""

    def select(
        self, user_message: str, *, k: int = 3, meta: Optional[Dict[str, Any]] = None
    ) -> "List[GuidanceCard]":
        """Top-``k`` cards (WITH bodies) most relevant to ``user_message``; may return [].
        ``meta`` carries the caller's scope (e.g. user/team/quest ids). Never raises."""


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
    def __init__(self):
        self.call_count: int = 0  # Track LLM calls for reporting

    @abc.abstractmethod
    def plan(self, prompt, *, model, tool_schema) -> Dict[str, Any]: ...
    @abc.abstractmethod
    def answer(self, messages, *, model, system=None) -> str: ...
    @abc.abstractmethod
    def list_models(self) -> List[str]: ...

    # Optional NATIVE web-search capability. Non-abstract so existing providers keep
    # satisfying the ABC; a provider whose SDK ships a web-search tool overrides both.
    def supports_web_search(self, model: Optional[str] = None) -> bool:
        return False

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        raise NotImplementedError("web search not supported by this provider")


class DeepRunnerBase(abc.ABC):
    @abc.abstractmethod
    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None,
                 context_preamble=None, run_id=None) -> DeepResult: ...


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
    """BACKGROUND sink — surface ONLY result / decision / milestone / context / done.

    Planning, reading, re-planning, status ticks, and partial chunks are dropped: nobody is
    attending, so intermediate chatter is noise. The same surfacing set (``SURFACING_EVENTS``)
    is enforced here once. Construct with optional callables for each surfacing kind; the
    runner's executor wires these to (optional progress note / Quest decision-request / final
    PATCH). Any unset callable means "that kind is simply not forwarded anywhere."
    """

    def __init__(
        self,
        *,
        on_context: Optional[Callable[[ProgressEvent], None]] = None,
        on_milestone: Optional[Callable[[ProgressEvent], None]] = None,
        on_decision: Optional[Callable[[ProgressEvent], None]] = None,
        on_result: Optional[Callable[[ProgressEvent], None]] = None,
        on_done: Optional[Callable[[ProgressEvent], None]] = None,
        on_tokens: Optional[Callable[[ProgressEvent], None]] = None,
        on_overseer: Optional[Callable[[ProgressEvent], None]] = None,
    ):
        self._on_context = on_context
        self._on_milestone = on_milestone
        self._on_decision = on_decision
        self._on_result = on_result
        self._on_done = on_done
        self._on_tokens = on_tokens
        self._on_overseer = on_overseer

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        if event.type not in SURFACING_EVENTS:
            return  # drop planning/reading/re-planning/status/partial chatter
        try:
            if event.type == EVENT_CONTEXT and self._on_context:
                self._on_context(event)
            elif event.type == EVENT_MILESTONE and self._on_milestone:
                self._on_milestone(event)
            elif event.type == EVENT_DECISION and self._on_decision:
                self._on_decision(event)
            elif event.type == EVENT_RESULT and self._on_result:
                self._on_result(event)
            elif event.type == EVENT_DONE and self._on_done:
                self._on_done(event)
            elif event.type == EVENT_TOKENS and self._on_tokens:
                self._on_tokens(event)
            elif event.type == EVENT_OVERSEER and self._on_overseer:
                self._on_overseer(event)
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
