"""Tests for WebSearchAdapter -- offline (no network, no real API key)."""
from __future__ import annotations

import json
import urllib.error
import unittest.mock as mock
from unittest.mock import patch, MagicMock

import pytest

from quest_ai_runner.adapters import WebSearchAdapter, CompositeRetrievalAdapter, FilesAdapter
from quest_ai_runner.core.adapters import Observation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_search_resp(answer: str = "", results=None):
    """Build a mock Tavily /search response dict."""
    return {
        "answer": answer,
        "results": results or [
            {
                "title": "Best Keyboards 2024",
                "url": "https://example.com/keyboards",
                "content": "Top 10 mechanical keyboards under $150.",
            }
        ],
    }


def _make_extract_resp(content: str = "Page content here."):
    return {
        "results": [
            {
                "url": "https://example.com",
                "content": content,
                "raw_content": content,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------

def test_construction_with_api_key():
    adapter = WebSearchAdapter(api_key="tvly_test_key", max_results=3)
    assert adapter._api_key == "tvly_test_key"
    assert adapter._max_results == 3


def test_construction_from_env(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly_from_env")
    adapter = WebSearchAdapter()
    assert adapter._api_key == "tvly_from_env"


def test_missing_api_key_returns_error_not_raise():
    adapter = WebSearchAdapter(api_key="")
    obs = adapter.query({"q": "test"})
    assert obs.kind == "error"
    assert "WEB_SEARCH_API_KEY" in (obs.error or "")


# ---------------------------------------------------------------------------
# query() tests
# ---------------------------------------------------------------------------

def test_query_success():
    adapter = WebSearchAdapter(api_key="tvly_key")
    mock_resp = _make_search_resp(answer="The best keyboard is the Keychron Q1.", results=[
        {"title": "Keychron Q1 Review", "url": "https://rtings.com/q1", "content": "Great keyboard."}
    ])
    with patch.object(adapter, "_post_json", return_value=mock_resp):
        obs = adapter.query({"q": "best mechanical keyboard under 150"})

    assert obs.kind == "query"
    assert obs.text is not None
    assert "Keychron Q1" in obs.text
    assert "rtings.com" in obs.text


def test_query_includes_answer_when_present():
    adapter = WebSearchAdapter(api_key="tvly_key")
    mock_resp = _make_search_resp(answer="Use the Keychron Q1 for best value.")
    with patch.object(adapter, "_post_json", return_value=mock_resp):
        obs = adapter.query({"q": "keyboard recommendation"})

    assert "Keychron Q1" in (obs.text or "")
    assert "Web search answer" in (obs.text or "")


def test_query_with_scope():
    adapter = WebSearchAdapter(api_key="tvly_key")
    captured = {}

    def capture_post(url, payload):
        captured["query"] = payload.get("query", "")
        return _make_search_resp()

    with patch.object(adapter, "_post_json", side_effect=capture_post):
        adapter.query({"q": "mechanical keyboard", "scope": "site:amazon.com"})

    assert "site:amazon.com" in captured["query"]


def test_query_missing_q_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.query({"filter": "something"})
    assert obs.kind == "error"
    assert "q" in (obs.error or "").lower() or "query" in (obs.error or "").lower()


def test_query_empty_results_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    with patch.object(adapter, "_post_json", return_value={"answer": "", "results": []}):
        obs = adapter.query({"q": "very obscure thing"})
    assert obs.kind == "error"


def test_query_http_401_returns_friendly_error():
    adapter = WebSearchAdapter(api_key="tvly_bad_key")

    def raise_401(url, payload):
        err = urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        err.read = lambda: b'{"error":"unauthorized"}'
        raise err

    with patch.object(adapter, "_post_json", side_effect=raise_401):
        obs = adapter.query({"q": "test"})

    assert obs.kind == "error"
    assert "401" in (obs.error or "") or "invalid" in (obs.error or "").lower()


def test_query_http_429_returns_rate_limit_error():
    adapter = WebSearchAdapter(api_key="tvly_key")

    def raise_429(url, payload):
        err = urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        err.read = lambda: b'{"error":"rate_limit"}'
        raise err

    with patch.object(adapter, "_post_json", side_effect=raise_429):
        obs = adapter.query({"q": "test"})

    assert obs.kind == "error"
    assert "429" in (obs.error or "") or "rate limit" in (obs.error or "").lower()


def test_query_network_error_returns_error_not_raise():
    adapter = WebSearchAdapter(api_key="tvly_key")
    with patch.object(adapter, "_post_json", side_effect=ConnectionError("timeout")):
        obs = adapter.query({"q": "test"})
    assert obs.kind == "error"
    assert "timeout" in (obs.error or "").lower() or "error" in (obs.error or "").lower()


# ---------------------------------------------------------------------------
# grep() tests
# ---------------------------------------------------------------------------

def test_grep_success():
    adapter = WebSearchAdapter(api_key="tvly_key")
    mock_resp = _make_search_resp(results=[
        {"title": "Top Keyboards", "url": "https://ex.com/kbs", "content": "Keychron is great."}
    ])
    with patch.object(adapter, "_post_json", return_value=mock_resp):
        obs = adapter.grep("mechanical keyboard review")

    assert obs.kind == "grep"
    assert len(obs.hits) > 0
    assert "url" in obs.hits[0]


def test_grep_with_scope_appended():
    adapter = WebSearchAdapter(api_key="tvly_key")
    captured = {}

    def capture_post(url, payload):
        captured["query"] = payload.get("query", "")
        return _make_search_resp()

    with patch.object(adapter, "_post_json", side_effect=capture_post):
        adapter.grep("keyboard", scope="site:amazon.com")

    assert "site:amazon.com" in captured["query"]


def test_grep_no_results_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    with patch.object(adapter, "_post_json", return_value={"results": []}):
        obs = adapter.grep("nonexistent_pattern_xyz")
    assert obs.kind == "error"


def test_grep_missing_key_returns_error():
    adapter = WebSearchAdapter(api_key="")
    obs = adapter.grep("test")
    assert obs.kind == "error"


# ---------------------------------------------------------------------------
# read_section() tests
# ---------------------------------------------------------------------------

def test_read_section_url_fetches_content():
    adapter = WebSearchAdapter(api_key="tvly_key")
    with patch.object(adapter, "_post_json", return_value=_make_extract_resp("Page content here.")):
        obs = adapter.read_section("https://example.com/page")

    assert obs.kind == "read"
    assert "Page content" in (obs.text or "")


def test_read_section_non_url_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.read_section("some/local/path.md")
    assert obs.kind == "error"
    assert "URL" in (obs.error or "") or "http" in (obs.error or "").lower()


def test_read_section_missing_key_returns_error():
    adapter = WebSearchAdapter(api_key="")
    obs = adapter.read_section("https://example.com")
    assert obs.kind == "error"


def test_read_section_max_bytes_truncates():
    adapter = WebSearchAdapter(api_key="tvly_key")
    long_content = "x" * 10000
    with patch.object(adapter, "_post_json", return_value=_make_extract_resp(long_content)):
        obs = adapter.read_section("https://example.com", max_bytes=100)

    assert obs.kind == "read"
    assert len((obs.text or "").encode("utf-8")) <= 100


# ---------------------------------------------------------------------------
# Discovery methods
# ---------------------------------------------------------------------------

def test_list_sources_returns_web_entry():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.list_sources()
    assert obs.kind == "query"
    assert "web" in (obs.text or "").lower()


def test_describe_source_web():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.describe_source("web")
    assert obs.kind == "query"
    assert "Tavily" in (obs.text or "")


def test_describe_source_unknown_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.describe_source("files")
    assert obs.kind == "error"


def test_list_operations():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.list_operations()
    assert obs.kind == "query"
    assert "web_search" in (obs.text or "")
    assert "web_grep" in (obs.text or "")
    assert "web_fetch" in (obs.text or "")


def test_describe_operation_web_search():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.describe_operation("web_search")
    assert obs.kind == "query"
    assert "query" in (obs.text or "").lower()


def test_describe_operation_unknown_returns_error():
    adapter = WebSearchAdapter(api_key="tvly_key")
    obs = adapter.describe_operation("unknown_op")
    assert obs.kind == "error"


# ---------------------------------------------------------------------------
# Integration with CompositeRetrievalAdapter
# ---------------------------------------------------------------------------

def test_composite_with_web_adapter():
    """WebSearchAdapter participates in CompositeRetrievalAdapter correctly."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a file so FilesAdapter has something to grep
        (pathlib.Path(tmpdir) / "readme.md").write_text("This is a local file.")

        files = FilesAdapter(tmpdir)
        web = WebSearchAdapter(api_key="tvly_key")
        composite = CompositeRetrievalAdapter([files, web])

        # list_sources should include web
        obs = composite.list_sources()
        assert obs.kind == "query"
        assert "web" in (obs.text or "").lower()


def test_web_adapter_never_raises():
    """No public method on WebSearchAdapter should raise; all return Observation."""
    adapter = WebSearchAdapter(api_key="")

    # These should all return Observations, never raise
    assert isinstance(adapter.query({}), Observation)
    assert isinstance(adapter.grep(""), Observation)
    assert isinstance(adapter.read_section("not_a_url"), Observation)
    assert isinstance(adapter.list_sources(), Observation)
    assert isinstance(adapter.describe_source("x"), Observation)
    assert isinstance(adapter.list_operations(), Observation)
    assert isinstance(adapter.describe_operation("x"), Observation)
