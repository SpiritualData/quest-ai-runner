"""Tests for web_search_backends -- offline (fake http callables / fake genai client)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.web_search_backends import (
    BraveBackend,
    FallbackSearchBackend,
    GeminiGroundingBackend,
    ProviderNativeBackend,
    SearchBackendError,
    SearchHit,
    SearchResponse,
    SearxngBackend,
    SerperBackend,
    TavilyBackend,
    select_search_backend,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Optional[Dict[str, Any]] = None, headers=None):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.headers = headers or {}

    def json(self):
        return self._json_body


# ---------------------------------------------------------------------------
# Serper
# ---------------------------------------------------------------------------


def test_serper_parses_organic_and_answer_box():
    def fake_http(method, url, **kwargs):
        assert method == "POST"
        assert "serper.dev" in url
        assert kwargs["headers"]["X-API-KEY"] == "sk-serper"
        return FakeResponse(
            json_body={
                "organic": [
                    {"title": "Keyboards 2024", "link": "https://example.com/kb", "snippet": "Top picks.", "date": "2024-01-01"},
                ],
                "answerBox": {"answer": "The best keyboard is the X1."},
            }
        )

    backend = SerperBackend(api_key="sk-serper", http=fake_http)
    resp = backend.search("best keyboards", max_results=5)
    assert resp.answer == "The best keyboard is the X1."
    assert len(resp.hits) == 1
    assert resp.hits[0].title == "Keyboards 2024"
    assert resp.hits[0].url == "https://example.com/kb"


def test_serper_answer_falls_back_to_knowledge_graph():
    def fake_http(method, url, **kwargs):
        return FakeResponse(json_body={"organic": [], "knowledgeGraph": {"description": "A KG description."}})

    backend = SerperBackend(api_key="k", http=fake_http)
    resp = backend.search("q")
    assert resp.answer == "A KG description."


def test_serper_429_raises_with_status_code():
    def fake_http(method, url, **kwargs):
        return FakeResponse(status_code=429)

    backend = SerperBackend(api_key="k", http=fake_http)
    with pytest.raises(SearchBackendError) as exc_info:
        backend.search("q")
    assert exc_info.value.status_code == 429
    assert "429" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Brave
# ---------------------------------------------------------------------------


def test_brave_strips_html_tags_from_description():
    def fake_http(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["headers"]["X-Subscription-Token"] == "brave-key"
        return FakeResponse(
            json_body={
                "web": {
                    "results": [
                        {
                            "title": "Result One",
                            "url": "https://example.com/one",
                            "description": "Some <strong>bold</strong> text here.",
                            "age": "2 days ago",
                        }
                    ]
                }
            }
        )

    backend = BraveBackend(api_key="brave-key", http=fake_http)
    resp = backend.search("q")
    assert resp.hits[0].snippet == "Some bold text here."
    assert resp.hits[0].date == "2 days ago"


def test_brave_401_raises():
    def fake_http(method, url, **kwargs):
        return FakeResponse(status_code=401)

    backend = BraveBackend(api_key="bad", http=fake_http)
    with pytest.raises(SearchBackendError) as exc_info:
        backend.search("q")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


def test_tavily_parses_results_and_answer_uses_bearer_header():
    captured = {}

    def fake_http(method, url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return FakeResponse(
            json_body={
                "answer": "Tavily synthesized answer.",
                "results": [
                    {"title": "T1", "url": "https://example.com/t1", "content": "Content snippet.", "published_date": "2024-02-02"},
                ],
            }
        )

    backend = TavilyBackend(api_key="tvly-key", http=fake_http)
    resp = backend.search("q")
    assert resp.answer == "Tavily synthesized answer."
    assert resp.hits[0].url == "https://example.com/t1"
    assert captured["headers"]["Authorization"] == "Bearer tvly-key"


# ---------------------------------------------------------------------------
# SearXNG
# ---------------------------------------------------------------------------


def test_searxng_parses_results():
    def fake_http(method, url, **kwargs):
        assert url == "http://localhost:8080/search"
        return FakeResponse(
            json_body={"results": [{"title": "S1", "url": "https://example.com/s1", "content": "c", "publishedDate": "2024-03-03"}]}
        )

    backend = SearxngBackend(base_url="http://localhost:8080", http=fake_http)
    resp = backend.search("q")
    assert resp.hits[0].title == "S1"


def test_searxng_no_results_raises():
    def fake_http(method, url, **kwargs):
        return FakeResponse(json_body={"results": []})

    backend = SearxngBackend(base_url="http://localhost:8080", http=fake_http)
    with pytest.raises(SearchBackendError):
        backend.search("q")


# ---------------------------------------------------------------------------
# GeminiGroundingBackend: grounding-metadata parsing + redirect resolution
# ---------------------------------------------------------------------------


def _make_fake_genai_response(text="Grounded answer.", with_metadata=True):
    if not with_metadata:
        return SimpleNamespace(text=text, usage_metadata=None, candidates=[])

    chunk0 = SimpleNamespace(web=SimpleNamespace(uri="https://vertexaisearch.example/redirect/0", title="example.com"))
    chunk1 = SimpleNamespace(web=SimpleNamespace(uri="https://vertexaisearch.example/redirect/1", title="other.com"))
    support0 = SimpleNamespace(
        segment=SimpleNamespace(text="The key fact from source zero."),
        grounding_chunk_indices=[0],
    )
    support1 = SimpleNamespace(
        segment=SimpleNamespace(text="A fact from source one."),
        grounding_chunk_indices=[1],
    )
    gm = SimpleNamespace(
        grounding_chunks=[chunk0, chunk1],
        grounding_supports=[support0, support1],
        web_search_queries=["best keyboards 2024"],
    )
    cand = SimpleNamespace(grounding_metadata=gm)
    usage = SimpleNamespace(prompt_token_count=120, candidates_token_count=45)
    return SimpleNamespace(text=text, usage_metadata=usage, candidates=[cand])


class _FakeModels:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        return self._response


class _FakeGenaiClient:
    def __init__(self, response):
        self.models = _FakeModels(response)


def test_gemini_grounding_parses_snippets_from_supports_and_resolves_redirects():
    response = _make_fake_genai_response()
    client = _FakeGenaiClient(response)

    def fake_http(method, url, **kwargs):
        if "redirect/0" in url:
            return FakeResponse(status_code=301, headers={"Location": "https://real-source.com/article-0"})
        if "redirect/1" in url:
            return FakeResponse(status_code=301, headers={"Location": "https://real-source.com/article-1"})
        return FakeResponse(status_code=404)

    backend = GeminiGroundingBackend(api_key="gm-key", client=client, http=fake_http)
    resp = backend.search("best keyboards 2024", max_results=5)

    assert resp.answer == "Grounded answer."
    assert resp.queries_issued == ["best keyboards 2024"]
    urls = {h.url for h in resp.hits}
    assert "https://real-source.com/article-0" in urls
    assert "https://real-source.com/article-1" in urls
    snippets = {h.url: h.snippet for h in resp.hits}
    assert "key fact from source zero" in snippets["https://real-source.com/article-0"]
    assert backend.calls == 1
    assert backend.tokens_in == 120
    assert backend.tokens_out == 45


def test_gemini_grounding_keeps_redirect_url_when_resolution_fails():
    response = _make_fake_genai_response()
    client = _FakeGenaiClient(response)

    def failing_http(method, url, **kwargs):
        raise ConnectionError("network unreachable")

    backend = GeminiGroundingBackend(api_key="gm-key", client=client, http=failing_http)
    resp = backend.search("q")
    urls = {h.url for h in resp.hits}
    assert "https://vertexaisearch.example/redirect/0" in urls
    assert "https://vertexaisearch.example/redirect/1" in urls


def test_gemini_grounding_no_metadata_still_returns_answer():
    response = _make_fake_genai_response(with_metadata=False)
    client = _FakeGenaiClient(response)
    backend = GeminiGroundingBackend(api_key="gm-key", client=client, http=lambda *a, **k: FakeResponse())
    resp = backend.search("q")
    assert resp.answer == "Grounded answer."
    assert resp.hits == []


def test_gemini_grounding_no_answer_no_hits_raises():
    response = _make_fake_genai_response(text="", with_metadata=False)
    client = _FakeGenaiClient(response)
    backend = GeminiGroundingBackend(api_key="gm-key", client=client, http=lambda *a, **k: FakeResponse())
    with pytest.raises(SearchBackendError):
        backend.search("q")


def test_gemini_grounding_requires_api_key_without_injected_client(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    backend = GeminiGroundingBackend(api_key="")
    with pytest.raises(SearchBackendError):
        backend._get_client()


# ---------------------------------------------------------------------------
# ProviderNativeBackend
# ---------------------------------------------------------------------------


def test_provider_native_wraps_provider_web_search():
    class FakeProvider:
        def web_search(self, query, *, model, max_results):
            return {"answer": "native answer", "results": [{"title": "N1", "url": "https://example.com/n1", "snippet": "s"}]}

    backend = ProviderNativeBackend(FakeProvider(), model="some-model")
    resp = backend.search("q")
    assert resp.answer == "native answer"
    assert resp.hits[0].url == "https://example.com/n1"


def test_provider_native_propagates_failure_as_search_backend_error():
    class FailingProvider:
        def web_search(self, query, *, model, max_results):
            raise RuntimeError("boom")

    backend = ProviderNativeBackend(FailingProvider(), model="m")
    with pytest.raises(SearchBackendError):
        backend.search("q")


# ---------------------------------------------------------------------------
# FallbackSearchBackend: fallback on error + cooldown
# ---------------------------------------------------------------------------


class _ScriptedBackend:
    name_counter = 0

    def __init__(self, name, outcomes):
        self.name = name
        self._outcomes = list(outcomes)
        self.call_count = 0

    def search(self, query, *, max_results=5):
        self.call_count += 1
        # Pop through the scripted outcomes; once exhausted, keep repeating the last one
        # (a real backend doesn't just stop responding after N calls).
        if len(self._outcomes) > 1:
            outcome = self._outcomes.pop(0)
        elif self._outcomes:
            outcome = self._outcomes[0]
        else:
            outcome = SearchBackendError(f"{self.name}: exhausted")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fallback_moves_to_next_backend_on_error():
    good_resp = SearchResponse(hits=[SearchHit(title="ok", url="https://example.com/ok")], answer="")
    b1 = _ScriptedBackend("b1", [SearchBackendError("b1: HTTP 500")])
    b2 = _ScriptedBackend("b2", [good_resp])
    fb = FallbackSearchBackend([b1, b2])
    resp = fb.search("q")
    assert resp is good_resp
    assert fb.name == "b1>b2"


def test_fallback_skips_backend_in_cooldown_after_429():
    good_resp = SearchResponse(hits=[SearchHit(title="ok", url="https://example.com/ok")], answer="")
    b1 = _ScriptedBackend("b1", [SearchBackendError("b1: HTTP 429 rate limited", status_code=429), good_resp])
    b2 = _ScriptedBackend("b2", [good_resp])
    fb = FallbackSearchBackend([b1, b2], cooldown_seconds=60.0)

    # First call: b1 fails with 429 (-> cooldown), falls through to b2.
    resp1 = fb.search("q1")
    assert resp1 is good_resp
    assert b2.call_count == 1

    # Second call: b1 should be skipped (in cooldown) without being called again.
    resp2 = fb.search("q2")
    assert resp2 is good_resp
    assert b1.call_count == 1  # not called a second time
    assert b2.call_count == 2


def test_fallback_raises_when_all_backends_fail():
    b1 = _ScriptedBackend("b1", [SearchBackendError("b1: down")])
    b2 = _ScriptedBackend("b2", [SearchBackendError("b2: down")])
    fb = FallbackSearchBackend([b1, b2])
    with pytest.raises(SearchBackendError):
        fb.search("q")


def test_fallback_requires_at_least_one_backend():
    with pytest.raises(ValueError):
        FallbackSearchBackend([])


# ---------------------------------------------------------------------------
# select_search_backend: disabled / explicit / missing key / auto order / fallback chain
# ---------------------------------------------------------------------------


def test_select_disabled_returns_none():
    env = {"WEB_SEARCH_ENABLED": "false", "SERPER_API_KEY": "k"}
    assert select_search_backend(env) is None


def test_select_explicit_backend_used():
    env = {"QAR_WEB_SEARCH_BACKEND": "brave", "BRAVE_SEARCH_API_KEY": "k", "SERPER_API_KEY": "also-present"}
    backend = select_search_backend(env)
    assert backend.name == "brave"


def test_select_explicit_backend_missing_key_returns_none():
    env = {"QAR_WEB_SEARCH_BACKEND": "serper"}
    assert select_search_backend(env) is None


def test_select_auto_order_prefers_serper_over_brave():
    env = {"SERPER_API_KEY": "s", "BRAVE_SEARCH_API_KEY": "b"}
    backend = select_search_backend(env)
    assert backend.name == "serper>brave"


def test_select_auto_single_backend_not_wrapped_in_fallback():
    env = {"BRAVE_SEARCH_API_KEY": "b"}
    backend = select_search_backend(env)
    assert backend.name == "brave"


def test_select_auto_no_config_returns_none():
    assert select_search_backend({}) is None


def test_select_provider_native_requires_model_and_support():
    class SupportingProvider:
        def supports_web_search(self, model):
            return model == "good-model"

    env = {"QAR_WEB_SEARCH_BACKEND": "provider", "QAR_WEB_SEARCH_PROVIDER_MODEL": "good-model"}
    backend = select_search_backend(env, provider=SupportingProvider())
    assert backend is not None
    assert backend.name == "provider-native"

    env_unsupported = {"QAR_WEB_SEARCH_BACKEND": "provider", "QAR_WEB_SEARCH_PROVIDER_MODEL": "bad-model"}
    assert select_search_backend(env_unsupported, provider=SupportingProvider()) is None
