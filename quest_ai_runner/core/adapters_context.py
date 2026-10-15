"""Ports and value objects for context: assembled context, conversations, ``ContextAssembler`` and ``VectorStore``.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from dataclasses import dataclass, field


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
      ``partial``           -- True when the assembler ran out of budget and returned only what
                               completed in time (see ``meta["assembly_deadline"]``, honored by
                               deadline-aware assemblers such as ``HybridContextAssembler``).
                               A partial result is still real context -- the Orchestrator
                               prefers it over dropping the turn's fresh context entirely --
                               but consumers can surface that it was incomplete.  Defaults to
                               False (backward compatible).
    """
    context_view: str = ""
    model_tier_hint: Optional[str] = None
    card_ids: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    card_metadata: List[Dict[str, Any]] = field(default_factory=list)  # [{'id', 'title', 'relevance_score', 'file_count', 'files', 'adapter'}]
    partial: bool = False


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
