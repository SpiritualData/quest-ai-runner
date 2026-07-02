"""WebSearchAdapter -- RetrievalAdapter that queries the live web via Tavily.

This lets the shallow orchestrator ground answers in current web data without
needing a full deep / Claude Code run. The planner can call ``query`` with a
search spec and receive a synthesized result from the top-N web pages.

Configuration (env vars or pass at construction):
  WEB_SEARCH_API_KEY   -- Tavily API key (tvly_...). Required when enabled.
  WEB_SEARCH_MAX_RESULTS (optional) -- max search results returned (default 5).

The adapter is graceful: every public method catches all exceptions and returns
an Observation(kind="error", ...) rather than raising, so a misconfigured or
unreachable Tavily endpoint never breaks the orchestrator loop.

HTTP calls use only stdlib (urllib.request + json) -- no third-party deps.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase
from .web_query_spec import coerce_web_query

_log = logging.getLogger("quest-ai-runner.web-search")

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


class WebSearchAdapter(RetrievalAdapterBase):
    """RetrievalAdapter that searches the live web via the Tavily API.

    Wire it into a CompositeRetrievalAdapter alongside FilesAdapter so the
    orchestrator can ground on both the local corpus and the live web:

        from quest_ai_runner.adapters import (
            CompositeRetrievalAdapter, FilesAdapter, WebSearchAdapter
        )
        retrieval = CompositeRetrievalAdapter([
            FilesAdapter(corpus_root),
            WebSearchAdapter(api_key="tvly_..."),
        ])

    Or let the CLI wire it automatically via WEB_SEARCH_ENABLED + WEB_SEARCH_API_KEY.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        max_results: int = 5,
        timeout_seconds: float = 20.0,
        include_answer: bool = True,
        include_raw_content: bool = False,
        search_depth: str = "basic",
    ) -> None:
        """
        Args:
            api_key: Tavily API key (tvly_...). Falls back to WEB_SEARCH_API_KEY env var.
            max_results: Maximum results per search call (default 5).
            timeout_seconds: HTTP request timeout (default 20s).
            include_answer: Ask Tavily to include a synthesized answer (default True).
            include_raw_content: Include raw page HTML in results (default False).
            search_depth: "basic" (fast) or "advanced" (deeper but slower).
        """
        self._api_key = api_key or os.getenv("WEB_SEARCH_API_KEY", "")
        self._max_results = max_results
        self._timeout = timeout_seconds
        self._include_answer = include_answer
        self._include_raw_content = include_raw_content
        self._search_depth = search_depth

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST JSON to url and return the parsed response dict. May raise."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)

    def _search(self, query_text: str, max_results: Optional[int] = None) -> Dict[str, Any]:
        """Call the Tavily /search endpoint. Returns the raw API response."""
        payload: Dict[str, Any] = {
            "api_key": self._api_key,
            "query": query_text,
            "max_results": max_results or self._max_results,
            "search_depth": self._search_depth,
            "include_answer": self._include_answer,
            "include_raw_content": self._include_raw_content,
        }
        return self._post_json(_TAVILY_SEARCH_URL, payload)

    def _extract(self, urls: List[str]) -> Dict[str, Any]:
        """Call the Tavily /extract endpoint to fetch page content."""
        payload: Dict[str, Any] = {
            "api_key": self._api_key,
            "urls": urls,
        }
        return self._post_json(_TAVILY_EXTRACT_URL, payload)

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
        """Fetch page content from a URL.

        ``rel_path`` is treated as a URL when it starts with http:// or https://.
        For non-URL paths, returns an error (this adapter does not read local files).
        """
        try:
            url = (rel_path or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                return Observation(
                    kind="error",
                    rel_path=rel_path,
                    error="WebSearchAdapter.read_section: rel_path must be a URL (http:// or https://)",
                )
            if not self._api_key:
                return Observation(kind="error", rel_path=url, error="web search not configured: WEB_SEARCH_API_KEY missing")

            resp = self._extract([url])
            results = resp.get("results", [])
            if not results:
                return Observation(kind="error", rel_path=url, error=f"no content extracted from {url}")

            # Take the first result's raw content or main content
            first = results[0]
            content = first.get("raw_content") or first.get("content") or ""
            if not content:
                return Observation(kind="error", rel_path=url, error=f"empty content from {url}")

            # Apply max_bytes if requested
            if max_bytes and len(content.encode("utf-8")) > max_bytes:
                content = content.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")

            return Observation(
                kind="read",
                rel_path=url,
                locator=f"web extract: {url}",
                text=content,
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("web extract failed for %r: %s", rel_path, exc)
            return Observation(kind="error", rel_path=rel_path, error=f"web extract error: {exc}")

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Search the web for pages matching the pattern string.

        ``pattern`` is used as the search query. ``scope`` can narrow the search
        domain (e.g. "site:amazon.com"). Returns matched page titles + snippets as hits.
        """
        try:
            if not self._api_key:
                return Observation(kind="error", pattern=pattern, error="web search not configured: WEB_SEARCH_API_KEY missing")

            query = pattern
            if scope:
                query = f"{pattern} {scope}"

            n = max_hits or self._max_results
            resp = self._search(query, max_results=n)

            results = resp.get("results", [])
            if not results:
                return Observation(kind="error", pattern=pattern, error=f"no web results for: {pattern}")

            hits = []
            for r in results:
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("content", "")
                line = f"[{title}] {url}\n{snippet}".strip()
                hits.append({"line": line, "url": url, "title": title, "snippet": snippet})

            return Observation(kind="grep", pattern=pattern, hits=hits)
        except Exception as exc:  # noqa: BLE001
            _log.debug("web grep failed for %r: %s", pattern, exc)
            return Observation(kind="error", pattern=pattern, error=f"web search error: {exc}")

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Run a web search query from a spec dict.

        Recognized spec keys:
          ``q`` or ``query``  -- the search query string (required)
          ``max_results``     -- max results (overrides adapter default)
          ``scope``           -- optional domain scope (e.g. "site:amazon.com")
          ``topic``           -- optional topic hint appended to the query

        Returns a synthesized text combining Tavily's answer + top result summaries.
        """
        try:
            if not self._api_key:
                return Observation(kind="error", error="web search not configured: WEB_SEARCH_API_KEY missing")

            # Extract query from spec. The planner emits varied/nested shapes, e.g.
            # {"query": {"operation": "web_search", "params": {"query": "..."}}}, so coerce
            # robustly to a flat query string rather than passing a nested dict to Tavily.
            parsed = coerce_web_query(spec)
            q = parsed["q"]
            if not q:
                return Observation(kind="error", error="web search query: spec must have a 'q' or 'query' key")

            # Optional enrichments
            topic = parsed["topic"]
            scope = parsed["scope"]
            if topic:
                q = f"{q} {topic}"
            if scope:
                q = f"{q} {scope}"

            max_results = parsed["max_results"] or self._max_results
            resp = self._search(q, max_results=max_results)

            parts: List[str] = []

            # Tavily can return a synthesized answer
            answer = resp.get("answer", "")
            if answer:
                parts.append(f"Web search answer:\n{answer}")

            results = resp.get("results", [])
            if results:
                result_lines: List[str] = []
                for i, r in enumerate(results, 1):
                    title = r.get("title", "")
                    url = r.get("url", "")
                    snippet = r.get("content", "")
                    result_lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
                parts.append("Sources:\n" + "\n\n".join(result_lines))

            if not parts:
                return Observation(kind="error", error=f"no results for web search: {q}")

            return Observation(
                kind="query",
                text="\n\n".join(parts),
                rel_path=f"web_search:{q[:80]}",
            )
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            _log.debug("Tavily HTTP %d: %s", exc.code, body)
            if exc.code == 401:
                return Observation(kind="error", error="web search: invalid API key (401 Unauthorized)")
            if exc.code == 429:
                return Observation(kind="error", error="web search: rate limit exceeded (429). Retry later.")
            return Observation(kind="error", error=f"web search HTTP error {exc.code}: {body}")
        except Exception as exc:  # noqa: BLE001
            _log.debug("web query failed: %s", exc)
            return Observation(kind="error", error=f"web search error: {exc}")

    # ------------------------------------------------------------------
    # Discovery methods
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_sources",
            text=(
                "web: Search the live internet for up-to-date information, product pages, "
                "news, prices, documentation, and any public web content. "
                "Use the 'query' operation with a 'q' key to search."
            ),
        )

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        if "web" not in (name or "").lower():
            return Observation(
                kind="error",
                error=f"WebSearchAdapter: unknown source {name!r}. Only 'web' is available.",
            )
        return Observation(
            kind="query",
            locator=f"describe_source({name})",
            text=(
                "Source: web (live internet search via Tavily)\n"
                "Operations: query (web search), grep (pattern search), read_section (page fetch by URL)\n"
                "query spec: {\"q\": \"search terms\", \"max_results\": 5, \"scope\": \"site:amazon.com\"}\n"
                "grep: pass the search phrase as the pattern; pass scope= to limit to a domain.\n"
                "read_section: pass a full URL (https://...) as rel_path to fetch page content.\n"
                "Results include Tavily's synthesized answer plus per-page title, URL, and snippet."
            ),
        )

    def list_operations(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_operations",
            text=(
                "web_search: Search the live web; returns a synthesized answer + top page summaries.\n"
                "web_grep: Find pages matching a keyword/phrase; returns title + URL + snippet per hit.\n"
                "web_fetch: Fetch the full content of a URL (pass the URL as rel_path to read_section)."
            ),
        )

    def describe_operation(self, name: str) -> Observation:
        ops = {
            "web_search": (
                "web_search: Run a web search query and return a synthesized answer.\n"
                "Use: call query({\"q\": \"your search terms\", \"max_results\": 5})\n"
                "Optional keys: 'scope' (e.g. 'site:amazon.com'), 'topic' (appended to q).\n"
                "Returns: Tavily synthesized answer + per-page title, URL, and snippet."
            ),
            "web_grep": (
                "web_grep: Find web pages matching a pattern/phrase.\n"
                "Use: call grep(\"search terms\", scope=\"site:amazon.com\", max_hits=5)\n"
                "Returns: list of hits with title, URL, and content snippet per page."
            ),
            "web_fetch": (
                "web_fetch: Fetch the full text content of a web page.\n"
                "Use: call read_section(\"https://example.com/page\")\n"
                "Returns: extracted page text. Works best on article/documentation pages."
            ),
        }
        text = ops.get((name or "").lower().replace("-", "_").replace(" ", "_"))
        if not text:
            return Observation(
                kind="error",
                error=f"WebSearchAdapter: unknown operation {name!r}. Available: web_search, web_grep, web_fetch.",
            )
        return Observation(kind="query", locator=f"describe_operation({name})", text=text)
