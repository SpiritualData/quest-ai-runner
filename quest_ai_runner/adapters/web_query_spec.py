"""Robust extraction of a web-search query from a planner-produced spec dict.

The orchestrator forwards the planner's raw decision to ``RetrievalAdapter.query(spec)``.
For web search the planner is inconsistent about shape, e.g. it emits any of:

    {"q": "portland marathon 2026"}
    {"query": "portland marathon 2026"}
    {"query": {"q": "portland marathon 2026", "max_results": 5}}
    {"query": {"operation": "web_search", "params": {"query": "portland marathon 2026"}}}
    {"query": {"operation": "web_search", "q": "portland marathon 2026"}}

A naive ``spec.get("q") or spec.get("query")`` returns a nested dict for the last three,
which then gets passed to the search API as the query string and fails. ``coerce_web_query``
digs through these nested shapes and returns the flat pieces every web adapter needs.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Keys that hold the query text, in priority order; ``params``/``input`` nest deeper.
_QUERY_KEYS = ("q", "query", "search", "text", "prompt", "term", "terms", "params", "input")


def coerce_web_query(spec: Any) -> Dict[str, Any]:
    """Return ``{"q": str, "max_results": Optional[int], "scope": str, "topic": str}``.

    Never raises; returns an empty ``q`` when no query text can be found.
    """
    out: Dict[str, Any] = {"q": "", "max_results": None, "scope": "", "topic": ""}
    _seen_ids = set()

    def visit(obj: Any) -> None:
        if isinstance(obj, str):
            if not out["q"] and obj.strip():
                out["q"] = obj.strip()
            return
        if not isinstance(obj, dict):
            return
        oid = id(obj)
        if oid in _seen_ids:  # guard against pathological self-referential dicts
            return
        _seen_ids.add(oid)

        for k in ("max_results", "maxResults", "n", "limit", "top_k", "k"):
            if out["max_results"] is None:
                v = obj.get(k)
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    out["max_results"] = v
                elif isinstance(v, str) and v.isdigit():
                    out["max_results"] = int(v)
        for k in ("scope", "site", "domain", "domain_filter"):
            if not out["scope"] and isinstance(obj.get(k), str):
                out["scope"] = obj[k].strip()
        if not out["topic"] and isinstance(obj.get("topic"), str):
            out["topic"] = obj["topic"].strip()

        # Descend only the recognized query-bearing keys (q / query / search / params / ...),
        # never arbitrary keys, so an unrelated string field is not mistaken for the query.
        for k in _QUERY_KEYS:
            if not out["q"] and k in obj:
                visit(obj[k])

    visit(spec)
    return out


def coerce_web_query_text(spec: Any) -> str:
    """Convenience: just the query string (empty if none found)."""
    return coerce_web_query(spec)["q"]
