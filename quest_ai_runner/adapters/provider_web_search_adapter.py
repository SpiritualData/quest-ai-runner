"""ProviderWebSearchAdapter -- RetrievalAdapter that searches the live web via the
model provider's NATIVE web-search tool (Anthropic web_search / Gemini Google Search
grounding), using the same LLM API key the runner already has.

This is the key-free default web-search option: unlike WebSearchAdapter (which needs a
separate Tavily key), this adapter reuses the configured ModelProvider, so any runner
with an LLM key can ground answers on the live web with zero extra setup. It exposes the
exact same RetrievalAdapter surface (query / grep / discovery) as WebSearchAdapter, so the
orchestrator's planner discovers and uses "web" the same way regardless of the backend.

It is graceful: every public method catches all exceptions and returns an
Observation(kind="error", ...) rather than raising, so a provider without web support or a
transient network failure never breaks the orchestrator loop.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase
from .web_query_spec import coerce_web_query

_log = logging.getLogger("quest-ai-runner.provider-web-search")


class ProviderWebSearchAdapter(RetrievalAdapterBase):
    """RetrievalAdapter backed by a ModelProvider's native web-search capability.

        from quest_ai_runner.adapters import ProviderWebSearchAdapter
        retrieval = ProviderWebSearchAdapter(provider, model="gemini-3.5-flash")

    Or let ``build_orchestrator`` wire it automatically: when web search is not disabled and
    the configured provider reports ``supports_web_search()``, it is appended to the
    retrieval stack, with a Tavily WebSearchAdapter taking precedence when a key is present.
    """

    def __init__(
        self,
        provider: Any,
        *,
        model: str,
        max_results: int = 5,
    ) -> None:
        """
        Args:
            provider: a ModelProvider (usually the MultiProvider) with native web search.
            model: the model id to run the search with (routed by MultiProvider by name).
            max_results: max sources returned / server-side searches per query (default 5).
        """
        self._provider = provider
        self._model = model
        self._max_results = max_results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _available(self) -> bool:
        fn = getattr(self._provider, "supports_web_search", None)
        try:
            return bool(fn(self._model)) if callable(fn) else False
        except Exception:  # noqa: BLE001
            return False

    def _run(self, query_text: str, max_results: Optional[int] = None) -> Dict[str, Any]:
        return self._provider.web_search(
            query_text, model=self._model, max_results=max_results or self._max_results
        )

    def _format(self, result: Dict[str, Any]) -> str:
        parts: List[str] = []
        answer = (result or {}).get("answer", "")
        if answer:
            parts.append(f"Web search answer:\n{answer}")
        results = (result or {}).get("results", []) or []
        if results:
            lines: List[str] = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("snippet", "")
                line = f"{i}. {title}\n   {url}"
                if snippet:
                    line += f"\n   {snippet}"
                lines.append(line)
            parts.append("Sources:\n" + "\n\n".join(lines))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # RetrievalAdapter interface
    # ------------------------------------------------------------------

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Native web search synthesizes across pages; it does not fetch a single URL.

        Use ``query`` (or ``grep``) instead; the synthesized answer already folds in the
        top pages' content with their source URLs.
        """
        return Observation(
            kind="error",
            rel_path=rel_path,
            error=(
                "ProviderWebSearchAdapter does not fetch single pages. Use query({'q': ...}) "
                "to search the web; the result includes a synthesized answer plus source URLs."
            ),
        )

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Search the web for the pattern; return matched pages as hits (title + URL)."""
        try:
            if not self._available():
                return Observation(kind="error", pattern=pattern, error="native web search not available for this provider")
            query = f"{pattern} {scope}".strip() if scope else pattern
            result = self._run(query, max_results=max_hits or self._max_results)
            hits = []
            for r in (result.get("results", []) or []):
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("snippet", "")
                line = f"[{title}] {url}\n{snippet}".strip()
                hits.append({"line": line, "url": url, "title": title, "snippet": snippet})
            answer = result.get("answer", "")
            if not hits and not answer:
                return Observation(kind="error", pattern=pattern, error=f"no web results for: {pattern}")
            # If the provider returned an answer but no discrete source list, surface the answer.
            if not hits and answer:
                return Observation(kind="grep", pattern=pattern, hits=[{"line": answer, "url": "", "title": "", "snippet": answer}])
            return Observation(kind="grep", pattern=pattern, hits=hits)
        except Exception as exc:  # noqa: BLE001
            _log.debug("native web grep failed for %r: %s", pattern, exc)
            return Observation(kind="error", pattern=pattern, error=f"web search error: {exc}")

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Run a web search from a spec dict.

        Recognized keys: ``q`` / ``query`` / ``search`` (required), ``max_results``,
        ``scope`` (e.g. "site:amazon.com"), ``topic`` (appended to the query).
        """
        try:
            if not self._available():
                return Observation(kind="error", error="native web search not available for this provider")
            # The planner emits varied/nested spec shapes; coerce robustly to a flat query.
            parsed = coerce_web_query(spec)
            q = parsed["q"]
            if not q:
                return Observation(kind="error", error="web search query: spec must have a 'q' or 'query' key")
            if parsed["topic"]:
                q = f"{q} {parsed['topic']}"
            if parsed["scope"]:
                q = f"{q} {parsed['scope']}"
            max_results = parsed["max_results"] or self._max_results
            result = self._run(q, max_results=max_results)
            text = self._format(result)
            if not text:
                return Observation(kind="error", error=f"no results for web search: {q}")
            return Observation(kind="query", text=text, rel_path=f"web_search:{q[:80]}")
        except Exception as exc:  # noqa: BLE001
            _log.debug("native web query failed: %s", exc)
            return Observation(kind="error", error=f"web search error: {exc}")

    # ------------------------------------------------------------------
    # Discovery methods (mirror WebSearchAdapter so "web" reads the same to the planner)
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_sources",
            text=(
                "web: Search the live internet for up-to-date information, product pages, "
                "news, prices, events, documentation, and any public web content. "
                "Use the 'query' operation with a 'q' key to search."
            ),
        )

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        if "web" not in (name or "").lower():
            return Observation(
                kind="error",
                error=f"ProviderWebSearchAdapter: unknown source {name!r}. Only 'web' is available.",
            )
        return Observation(
            kind="query",
            locator=f"describe_source({name})",
            text=(
                "Source: web (live internet search via the model provider's native tool)\n"
                "Operations: query (web search), grep (pattern search)\n"
                "query spec: {\"q\": \"search terms\", \"max_results\": 5, \"scope\": \"site:amazon.com\"}\n"
                "grep: pass the search phrase as the pattern; pass scope= to limit to a domain.\n"
                "Results include a synthesized answer plus per-page title and URL."
            ),
        )

    def list_operations(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_operations",
            text=(
                "web_search: Search the live web; returns a synthesized answer + top page summaries.\n"
                "web_grep: Find pages matching a keyword/phrase; returns title + URL per hit."
            ),
        )

    def describe_operation(self, name: str) -> Observation:
        ops = {
            "web_search": (
                "web_search: Run a web search query and return a synthesized answer.\n"
                "Use: call query({\"q\": \"your search terms\", \"max_results\": 5})\n"
                "Optional keys: 'scope' (e.g. 'site:amazon.com'), 'topic' (appended to q).\n"
                "Returns: a synthesized answer plus per-page title and URL."
            ),
            "web_grep": (
                "web_grep: Find web pages matching a pattern/phrase.\n"
                "Use: call grep(\"search terms\", scope=\"site:amazon.com\", max_hits=5)\n"
                "Returns: list of hits with title and URL per page."
            ),
        }
        text = ops.get((name or "").lower().replace("-", "_").replace(" ", "_"))
        if not text:
            return Observation(
                kind="error",
                error=f"ProviderWebSearchAdapter: unknown operation {name!r}. Available: web_search, web_grep.",
            )
        return Observation(kind="query", locator=f"describe_operation({name})", text=text)
