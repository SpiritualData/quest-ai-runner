"""Offline tests for QuestContextAdapter and build_quest_resolvers.

All HTTP is mocked (monkeypatching the adapter's _post method or urllib.request.urlopen).
No real network calls, no real credentials.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from quest_ai_runner.adapters.quest_context_adapter import (
    QuestContextAdapter,
    _QuestCollectionResolver,
    _QuestQueryResolver,
    _get_api_key,
    _get_base_url,
    build_quest_resolvers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_200(context: str = "merged context text") -> MagicMock:
    """Build a mock urllib response that returns a 200 with the given context."""
    payload = json.dumps({"context": context, "sources": [], "sources_visited": [], "pending_envs": []})
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload.encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _adapter(base_url: str = "https://quest.example.com", api_key: str = "qsk_test") -> QuestContextAdapter:
    return QuestContextAdapter(base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------

def test_configured_true_when_base_and_key():
    a = QuestContextAdapter(base_url="https://quest.example.com", api_key="qsk_test")
    assert a.configured is True


def test_configured_false_when_no_key():
    a = QuestContextAdapter(base_url="https://quest.example.com", api_key="")
    assert a.configured is False


def test_configured_false_when_no_base_url():
    a = QuestContextAdapter(base_url="", api_key="qsk_test")
    assert a.configured is False


def test_from_env_reads_quest_api_url(monkeypatch):
    monkeypatch.setenv("QUEST_API_URL", "https://api.from-env.org")
    monkeypatch.setenv("QUEST_API_KEY", "qsk_envkey")
    monkeypatch.delenv("QUEST_BASE_URL", raising=False)
    a = QuestContextAdapter.from_env()
    assert a._base_url == "https://api.from-env.org"
    assert a._api_key == "qsk_envkey"
    assert a.configured is True


def test_from_env_reads_quest_base_url_as_fallback(monkeypatch):
    """QUEST_BASE_URL is accepted when QUEST_API_URL is absent."""
    monkeypatch.delenv("QUEST_API_URL", raising=False)
    monkeypatch.setenv("QUEST_BASE_URL", "https://base.from-env.org")
    monkeypatch.setenv("QUEST_API_KEY", "qsk_basefallback")
    a = QuestContextAdapter.from_env()
    assert a._base_url == "https://base.from-env.org"
    assert a.configured is True


def test_from_env_prefers_quest_api_url_over_quest_base_url(monkeypatch):
    monkeypatch.setenv("QUEST_API_URL", "https://api.preferred.org")
    monkeypatch.setenv("QUEST_BASE_URL", "https://base.not-used.org")
    monkeypatch.setenv("QUEST_API_KEY", "qsk_k")
    a = QuestContextAdapter.from_env()
    assert a._base_url == "https://api.preferred.org"


def test_env_helper_get_base_url_prefers_api_url(monkeypatch):
    monkeypatch.setenv("QUEST_API_URL", "https://api.example.org")
    monkeypatch.setenv("QUEST_BASE_URL", "https://base.example.org")
    assert _get_base_url() == "https://api.example.org"


def test_env_helper_get_base_url_falls_back_to_base_url(monkeypatch):
    monkeypatch.delenv("QUEST_API_URL", raising=False)
    monkeypatch.setenv("QUEST_BASE_URL", "https://base.example.org")
    assert _get_base_url() == "https://base.example.org"


def test_env_helper_get_api_key(monkeypatch):
    monkeypatch.setenv("QUEST_API_KEY", "qsk_abc123")
    assert _get_api_key() == "qsk_abc123"


def test_trailing_slash_stripped_from_base_url():
    a = QuestContextAdapter(base_url="https://quest.example.com/", api_key="qsk_test")
    assert not a._base_url.endswith("/")


# ---------------------------------------------------------------------------
# resolve() -- happy path
# ---------------------------------------------------------------------------

def test_resolve_returns_context_on_200():
    a = _adapter()
    with patch.object(a, "_post", return_value="my merged context"):
        result = a.resolve("what goals does the user have?")
    assert result == "my merged context"


def test_resolve_hits_correct_url():
    a = _adapter()
    captured: Dict[str, Any] = {}

    def fake_post(body: dict) -> str:
        captured["body"] = body
        return "ctx"

    with patch.object(a, "_post", side_effect=fake_post):
        a.resolve("test query", user_id="u123", quest_ids=["q1"], team_id="t1", max_chars=500)

    assert captured["body"]["query"] == "test query"
    assert captured["body"]["user_id"] == "u123"
    assert captured["body"]["quest_ids"] == ["q1"]
    assert captured["body"]["team_id"] == "t1"
    assert captured["body"]["max_chars"] == 500


def test_resolve_uses_default_scope_when_no_overrides():
    a = QuestContextAdapter(
        base_url="https://q.example.com",
        api_key="qsk_k",
        default_user_id="default_user",
        default_team_id="default_team",
        default_quest_ids=["default_q"],
    )
    captured: Dict[str, Any] = {}

    def fake_post(body: dict) -> str:
        captured["body"] = body
        return ""

    with patch.object(a, "_post", side_effect=fake_post):
        a.resolve("query without overrides")

    assert captured["body"]["user_id"] == "default_user"
    assert captured["body"]["team_id"] == "default_team"
    assert captured["body"]["quest_ids"] == ["default_q"]


def test_resolve_per_call_args_override_defaults():
    a = QuestContextAdapter(
        base_url="https://q.example.com",
        api_key="qsk_k",
        default_user_id="default_user",
    )
    captured: Dict[str, Any] = {}

    def fake_post(body: dict) -> str:
        captured["body"] = body
        return ""

    with patch.object(a, "_post", side_effect=fake_post):
        a.resolve("q", user_id="override_user")

    assert captured["body"]["user_id"] == "override_user"


def test_resolve_omits_optional_fields_when_absent():
    a = _adapter()
    captured: Dict[str, Any] = {}

    def fake_post(body: dict) -> str:
        captured["body"] = body
        return ""

    with patch.object(a, "_post", side_effect=fake_post):
        a.resolve("q")

    # Fields not supplied should not appear in the body
    assert "user_id" not in captured["body"]
    assert "quest_ids" not in captured["body"]
    assert "team_id" not in captured["body"]
    assert "max_chars" not in captured["body"]


# ---------------------------------------------------------------------------
# resolve() -- not configured
# ---------------------------------------------------------------------------

def test_resolve_returns_empty_when_not_configured():
    a = QuestContextAdapter(base_url="", api_key="")
    result = a.resolve("anything")
    assert result == ""


def test_resolve_returns_empty_when_no_api_key():
    a = QuestContextAdapter(base_url="https://q.example.com", api_key="")
    result = a.resolve("anything")
    assert result == ""


# ---------------------------------------------------------------------------
# resolve() -- error / graceful degradation
# ---------------------------------------------------------------------------

def test_resolve_returns_empty_on_http_error():
    a = _adapter()

    def raise_http(body):
        err = urllib.error.HTTPError("url", 500, "Server Error", {}, BytesIO(b"boom"))
        raise err

    with patch.object(a, "_post", side_effect=raise_http):
        result = a.resolve("query")
    assert result == ""


def test_resolve_returns_empty_on_exception():
    a = _adapter()
    with patch.object(a, "_post", side_effect=RuntimeError("network down")):
        result = a.resolve("query")
    assert result == ""


def test_resolve_returns_empty_on_timeout():
    a = _adapter()
    with patch.object(a, "_post", side_effect=TimeoutError("timed out")):
        result = a.resolve("query")
    assert result == ""


# ---------------------------------------------------------------------------
# _post() -- urllib integration (mocked at urlopen level)
# ---------------------------------------------------------------------------

def test_post_sends_correct_headers_and_body():
    a = _adapter(base_url="https://q.example.com", api_key="qsk_mykey")
    captured: Dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["auth"] = req.get_header("Authorization")
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = json.loads(req.data.decode())
        return _mock_200("body response")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a._post({"query": "test", "user_id": "u1"})

    assert captured["url"] == "https://q.example.com/api/quest-context/resolve"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer qsk_mykey"
    assert "application/json" in captured["content_type"].lower()
    assert captured["body"]["query"] == "test"
    assert result == "body response"


def test_post_returns_empty_on_http_error():
    a = _adapter()

    def fake_urlopen(req, timeout=None):
        err = urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, BytesIO(b"err"))
        raise err

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a._post({"query": "q"})
    assert result == ""


def test_post_returns_empty_on_url_error():
    a = _adapter()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a._post({"query": "q"})
    assert result == ""


def test_post_returns_empty_on_timeout():
    a = _adapter()

    def fake_urlopen(req, timeout=None):
        raise socket.timeout("timed out")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a._post({"query": "q"})
    assert result == ""


def test_post_returns_empty_on_json_parse_error():
    a = _adapter()
    bad_resp = MagicMock()
    bad_resp.read.return_value = b"not json {"
    bad_resp.__enter__ = lambda s: s
    bad_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=bad_resp):
        result = a._post({"query": "q"})
    assert result == ""


# ---------------------------------------------------------------------------
# ReferenceResolver implementations
# ---------------------------------------------------------------------------

class TestQuestCollectionResolver:
    def _resolver(self, return_value="hub context"):
        a = _adapter()
        with patch.object(a, "_post", return_value=return_value):
            # Patch _post so resolve() calls go straight through
            pass
        # Use a pre-patched adapter for the resolver
        a2 = _adapter()
        a2.resolve = lambda q, max_chars=2000: return_value  # type: ignore[method-assign]
        return _QuestCollectionResolver(a2)

    def test_resolve_combines_name_and_query(self):
        a = _adapter()
        called_with = {}

        def fake_resolve(q, max_chars=2000):
            called_with["q"] = q
            return "result"

        a.resolve = fake_resolve  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        result = r.resolve({"name": "goals", "query": "active goals"})
        assert result == "result"
        assert "goals" in called_with["q"]
        assert "active goals" in called_with["q"]

    def test_resolve_uses_collection_alias(self):
        a = _adapter()
        called_with = {}

        def fake_resolve(q, max_chars=2000):
            called_with["q"] = q
            return "ok"

        a.resolve = fake_resolve  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        r.resolve({"collection": "tasks", "query": "recent"})
        assert "tasks" in called_with["q"]

    def test_resolve_returns_empty_when_no_name_or_query(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: "should not be called"  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        result = r.resolve({})
        assert result == ""

    def test_resolve_returns_empty_on_adapter_failure(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: ""  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        result = r.resolve({"name": "goals", "query": "something"})
        assert result == ""

    def test_resolve_never_raises_on_bad_locator(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: "x"  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        # Should not raise even with completely wrong locator type
        result = r.resolve(None)  # type: ignore[arg-type]
        assert isinstance(result, str)

    def test_resolve_passes_max_chars_to_adapter(self):
        a = _adapter()
        called_with = {}

        def fake_resolve(q, max_chars=2000):
            called_with["max_chars"] = max_chars
            return "ok"

        a.resolve = fake_resolve  # type: ignore[method-assign]
        r = _QuestCollectionResolver(a)
        r.resolve({"name": "goals"}, max_chars=500)
        assert called_with["max_chars"] == 500


class TestQuestQueryResolver:
    def test_resolve_delegates_query_field(self):
        a = _adapter()
        called_with = {}

        def fake_resolve(q, max_chars=2000):
            called_with["q"] = q
            return "result"

        a.resolve = fake_resolve  # type: ignore[method-assign]
        r = _QuestQueryResolver(a)
        result = r.resolve({"query": "what are my goals?"})
        assert result == "result"
        assert called_with["q"] == "what are my goals?"

    def test_resolve_uses_text_alias(self):
        a = _adapter()
        called_with = {}

        def fake_resolve(q, max_chars=2000):
            called_with["q"] = q
            return "ok"

        a.resolve = fake_resolve  # type: ignore[method-assign]
        r = _QuestQueryResolver(a)
        r.resolve({"text": "my text query"})
        assert called_with["q"] == "my text query"

    def test_resolve_returns_empty_when_no_query(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: "should not be called"  # type: ignore[method-assign]
        r = _QuestQueryResolver(a)
        assert r.resolve({}) == ""

    def test_resolve_returns_empty_on_adapter_failure(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: ""  # type: ignore[method-assign]
        r = _QuestQueryResolver(a)
        assert r.resolve({"query": "something"}) == ""

    def test_resolve_never_raises_on_none_locator(self):
        a = _adapter()
        a.resolve = lambda q, max_chars=2000: "x"  # type: ignore[method-assign]
        r = _QuestQueryResolver(a)
        result = r.resolve(None)  # type: ignore[arg-type]
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# build_quest_resolvers()
# ---------------------------------------------------------------------------

def test_build_quest_resolvers_returns_collection_and_query_keys():
    a = _adapter()
    resolvers = build_quest_resolvers(a)
    assert "collection" in resolvers
    assert "query" in resolvers


def test_build_quest_resolvers_does_not_include_conversation():
    a = _adapter()
    resolvers = build_quest_resolvers(a)
    assert "conversation" not in resolvers


def test_build_quest_resolvers_collection_resolver_delegates():
    a = _adapter()
    calls = []

    def fake_resolve(q, max_chars=2000):
        calls.append(q)
        return "hub result"

    a.resolve = fake_resolve  # type: ignore[method-assign]
    resolvers = build_quest_resolvers(a)
    result = resolvers["collection"].resolve({"name": "goals", "query": "active"})
    assert result == "hub result"
    assert len(calls) == 1


def test_build_quest_resolvers_query_resolver_delegates():
    a = _adapter()
    calls = []

    def fake_resolve(q, max_chars=2000):
        calls.append(q)
        return "hub result"

    a.resolve = fake_resolve  # type: ignore[method-assign]
    resolvers = build_quest_resolvers(a)
    result = resolvers["query"].resolve({"query": "what did I do last week?"})
    assert result == "hub result"
    assert calls[0] == "what did I do last week?"


def test_build_quest_resolvers_can_merge_with_existing_dict():
    """Demonstrate that the returned dict can be merged into an existing resolver dict."""
    a = _adapter()
    existing = {"note": object()}
    resolvers = {**existing, **build_quest_resolvers(a)}
    assert "note" in resolvers
    assert "collection" in resolvers
    assert "query" in resolvers
