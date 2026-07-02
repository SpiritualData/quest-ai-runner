"""Tests for ProviderWebSearchAdapter -- offline (no network, no real API key).

Uses a stub ModelProvider whose web_search() returns a canned result, so we exercise the
RetrievalAdapter surface (query / grep / discovery / graceful degradation) deterministically.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters import ProviderWebSearchAdapter
from quest_ai_runner.core.adapters import Observation


class StubWebProvider:
    """A ModelProvider stub that supports native web search with a canned result."""

    def __init__(self, *, supported: bool = True, result: Optional[Dict[str, Any]] = None):
        self._supported = supported
        self._result = result if result is not None else {
            "answer": "Two marathons near Portland: Portland Marathon (Oct 4, 2026).",
            "results": [
                {"title": "Portland Marathon", "url": "https://www.portlandmarathon.com/", "snippet": ""},
                {"title": "RunGuides Portland", "url": "https://www.runguides.com/portland", "snippet": ""},
            ],
        }
        self.calls: List[str] = []

    def plan(self, prompt, *, model, tool_schema):  # pragma: no cover - unused here
        return {}

    def answer(self, messages, *, model, system=None):  # pragma: no cover - unused here
        return ""

    def list_models(self):  # pragma: no cover - unused here
        return ["gemini-3.5-flash"]

    def supports_web_search(self, model=None) -> bool:
        return self._supported

    def web_search(self, query: str, *, model: str, max_results: int = 5) -> Dict[str, Any]:
        self.calls.append(query)
        return self._result


def test_query_returns_answer_and_sources():
    adapter = ProviderWebSearchAdapter(StubWebProvider(), model="gemini-3.5-flash")
    obs = adapter.query({"q": "marathons near Portland Oregon 2026"})
    assert obs.kind == "query"
    assert "Portland Marathon" in obs.text
    assert "https://www.portlandmarathon.com/" in obs.text
    assert "Web search answer:" in obs.text


def test_query_applies_scope_and_topic():
    prov = StubWebProvider()
    adapter = ProviderWebSearchAdapter(prov, model="gemini-3.5-flash")
    adapter.query({"q": "running shoes", "scope": "site:amazon.com", "topic": "trail"})
    assert prov.calls[-1] == "running shoes trail site:amazon.com"


def test_query_handles_nested_planner_spec():
    # The shape that used to pass a dict to the provider and fail with a validation error.
    prov = StubWebProvider()
    adapter = ProviderWebSearchAdapter(prov, model="gemini-3.5-flash")
    obs = adapter.query({"query": {"operation": "web_search", "params": {"query": "marathons Portland"}}})
    assert obs.kind == "query"
    assert prov.calls[-1] == "marathons Portland"


def test_query_requires_a_query_key():
    obs = ProviderWebSearchAdapter(StubWebProvider(), model="m").query({"foo": "bar"})
    assert obs.kind == "error"
    assert "q" in (obs.error or "")


def test_grep_returns_hits():
    obs = ProviderWebSearchAdapter(StubWebProvider(), model="m").grep("marathon Portland")
    assert obs.kind == "grep"
    assert len(obs.hits) == 2
    assert obs.hits[0]["url"] == "https://www.portlandmarathon.com/"


def test_unsupported_provider_degrades_gracefully():
    adapter = ProviderWebSearchAdapter(StubWebProvider(supported=False), model="m")
    q = adapter.query({"q": "anything"})
    g = adapter.grep("anything")
    assert q.kind == "error" and "not available" in (q.error or "")
    assert g.kind == "error" and "not available" in (g.error or "")


def test_provider_exception_never_raises():
    class Boom(StubWebProvider):
        def web_search(self, query, *, model, max_results=5):
            raise RuntimeError("network down")

    obs = ProviderWebSearchAdapter(Boom(), model="m").query({"q": "x"})
    assert obs.kind == "error"
    assert "web search error" in (obs.error or "")


def test_read_section_directs_to_query():
    obs = ProviderWebSearchAdapter(StubWebProvider(), model="m").read_section("https://example.com")
    assert obs.kind == "error"
    assert "query" in (obs.error or "")


def test_discovery_advertises_web_source():
    adapter = ProviderWebSearchAdapter(StubWebProvider(), model="m")
    assert "web" in adapter.list_sources().text.lower()
    assert adapter.describe_source("web").kind == "query"
    assert adapter.describe_source("nope").kind == "error"
    assert "web_search" in adapter.list_operations().text
    assert adapter.describe_operation("web_search").kind == "query"


def test_answer_only_result_still_surfaces_in_grep():
    prov = StubWebProvider(result={"answer": "Just an answer, no sources.", "results": []})
    obs = ProviderWebSearchAdapter(prov, model="m").grep("something")
    assert obs.kind == "grep"
    assert obs.hits and "Just an answer" in obs.hits[0]["line"]
