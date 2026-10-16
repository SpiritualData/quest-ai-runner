"""Ports for gathering evidence: ``RetrievalAdapter`` and ``WebResearch``.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .adapters_values import Observation


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

    # --- OPTIONAL: PERSISTED REFERENCE RESOLUTION ------------------------------------
    # A context card can LEARN a source hit as a durable, typed REFERENCE that is re-resolved
    # FRESH every time the card is used (see adapters/reference_resolver.py +
    # adapters/card_scoped_learning.py) — never a stale snapshot. An adapter opts INTO being a
    # source whose content can be persisted-and-refetched this way by advertising a NON-None
    # ``reference_type`` (the content ``type`` its references register under, e.g. "conversation"
    # or "chat_thread") and implementing ``make_locator`` + ``resolve_reference``. This is the SAME
    # optional-capability convention as ``query`` above: an adapter that does NOT support it simply
    # leaves ``reference_type = None`` (the ``RetrievalAdapterBase`` default), and THAT is the
    # structural, checkable signal — ``adapter.reference_type is not None`` — no second Protocol and
    # no reading call chains to find out. ``resolve_reference`` mirrors ``ReferenceResolver.resolve``
    # (reference_resolver.py) exactly, so an adapter's own bound method drops STRAIGHT into
    # ``build_resolver_registry``'s ``consumer_resolvers`` as the resolver for its type — no wrapper.

    # The content ``type`` this adapter's learned references register under, or None (the default in
    # ``RetrievalAdapterBase``) when the adapter does not support persisted reference resolution.
    reference_type: Optional[str]

    def make_locator(self, candidate: Any) -> Dict[str, Any]:
        """Build the type-specific locator dict for ``candidate`` (one item this adapter surfaced),
        so ``card_scoped_learning.learn_card_references`` can persist it as a reference. Return ``{}``
        when the candidate cannot be located. Adapters that leave ``reference_type = None`` need not
        implement this. Never raises."""

    def resolve_reference(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> Optional[str]:
        """Re-fetch FRESH rendered text for a learned reference this adapter owns, given the
        ``locator`` built by ``make_locator``. Mirrors ``ReferenceResolver.resolve`` exactly (same
        signature, same contract): return the fresh text, or ``None`` / ``""`` on empty / failure /
        unsupported. NEVER raises — so the bound method can be wired directly as the
        ``ReferenceResolver`` for ``reference_type``. Default (unsupported): ``None``."""


@runtime_checkable
class WebResearch(Protocol):
    """Fast, token-efficient LIVE WEB access: the generic interface a consumer wires once (see
    hard rule #2) so the brain can read the public web the SAME way it reads any other source,
    without a special case inside ``core``.

    This is deliberately NOT folded into ``RetrievalAdapter``/``CompositeRetrievalAdapter``: that
    composite BROADCASTS every grep/query to every member adapter, which would fire a paid, slow
    web search on every ordinary corpus grep or DB query. ``Orchestrator.web`` is wired
    separately, and the planner reaches it only through its own two read keys (``{"web": ...}``,
    ``{"web_page": ...}``), dispatched by ``Orchestrator._exec_one_read`` before the retrieval
    adapter is even consulted.

    A reference implementation lives in ``adapters/web_research.py``
    (``build_web_research_from_env`` builds one from env: backend auto-selection, a snippet-first
    search, bounded page-extract fetch, and a cache). Never raises; a failure degrades to an
    ``Observation(kind="error", ...)``.
    """

    @property
    def backend_name(self) -> str:
        """Short id of the backend actually in use (e.g. "serper", "brave", "tavily",
        "searxng", "gemini", "provider"), for logging/status -- never user-facing prose."""

    def search(self, queries: Any, *, max_results: Optional[int] = None,
               fresh: bool = False) -> Observation:
        """Live web search for one query (``str``) or several (``List[str]``, run in parallel).

        Returns ``Observation(kind="query", rel_path="web_search:<q>", text=<compact results with
        a "cite as [title](url)" note>, hits=[{"title","url","snippet","query","source"}, ...])``.
        ``fresh`` bypasses any cache (facts that change by the hour). Never raises."""

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False,
              full: bool = False, part: Optional[int] = None) -> Observation:
        """Fetch ONE web page and extract the passages relevant to ``focus`` (or the page's own
        gist with no focus), bounded by the adapter's focused budget. ``full=True`` returns the
        whole document in order instead, one bounded part at a time; ``part=N`` (1-based) reads
        part N of a long one and the text says how to continue. The orchestrator passes ``full``
        and ``part`` only when the planner asked for them.

        Returns ``Observation(kind="read", rel_path=url, locator="web extract: <url>",
        text=<the relevant passages>)``. ``fresh`` bypasses the page cache. Never raises."""

    def describe(self) -> str:
        """One short line naming the backend/capability, for the planner prompt's WEB block
        (e.g. "web search (serper)"). Never raises."""
