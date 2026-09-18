"""Tests for GeminiProvider.supports_url_fetch() / fetch_url() -- offline (no network, no API key).

fetch_url() drives Gemini's url_context tool so GOOGLE retrieves one page server-side and returns
its text. These tests inject a fake google-genai client (same pattern as
tests/test_prompt_cache_layers.py's `_gemini_with_fake`) so no network call or real API key is
ever needed; only the (already-installed, offline-importable) `google.genai.types` module is used
to build the request, exactly as the real code does.
"""
from __future__ import annotations

import pytest

from quest_ai_runner.adapters.gemini_provider import GeminiProvider


class _FakeUrlMetadataEntry:
    """Mirrors one entry of response.candidates[i].url_context_metadata.url_metadata."""

    def __init__(self, status: str, retrieved_url: str):
        self.url_retrieval_status = status
        self.retrieved_url = retrieved_url


class _FakeUrlContextMetadata:
    def __init__(self, entries):
        self.url_metadata = entries


class _FakeCandidate:
    def __init__(self, url_context_metadata=None):
        self.url_context_metadata = url_context_metadata


class _FakeGeminiFetchResponse:
    def __init__(self, text: str, candidates=None, usage_metadata=None):
        self.text = text
        self.candidates = candidates or []
        self.usage_metadata = usage_metadata


class _FakeGeminiFetchModels:
    def __init__(self, response: _FakeGeminiFetchResponse):
        self._response = response
        self.last_kwargs = None

    def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeGeminiFetchClient:
    def __init__(self, response: _FakeGeminiFetchResponse):
        self.models = _FakeGeminiFetchModels(response)


def _provider_with_response(response: _FakeGeminiFetchResponse) -> GeminiProvider:
    p = GeminiProvider(api_key="offline")
    p._client = _FakeGeminiFetchClient(response)  # inject so _get_client() skips the real SDK
    return p


def _success_candidate(url: str) -> _FakeCandidate:
    entry = _FakeUrlMetadataEntry("URL_RETRIEVAL_STATUS_SUCCESS", url)
    return _FakeCandidate(_FakeUrlContextMetadata([entry]))


# --------------------------------------------------------------------------- #
# supports_url_fetch()
# --------------------------------------------------------------------------- #

def test_supports_url_fetch_true_with_api_key():
    assert GeminiProvider(api_key="k").supports_url_fetch() is True


def test_supports_url_fetch_false_without_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert GeminiProvider(api_key=None).supports_url_fetch() is False


# --------------------------------------------------------------------------- #
# fetch_url() -- success
# --------------------------------------------------------------------------- #

def test_fetch_url_success_returns_page_text():
    url = "https://example.com/article"
    resp = _FakeGeminiFetchResponse("The full page text.", candidates=[_success_candidate(url)])
    p = _provider_with_response(resp)

    result = p.fetch_url(url, model="gemini-3.5-flash")

    assert result == {
        "text": "The full page text.",
        "url": url,
        "status": "URL_RETRIEVAL_STATUS_SUCCESS",
    }


def test_fetch_url_uses_url_context_tool_and_default_instruction():
    url = "https://example.com/article"
    resp = _FakeGeminiFetchResponse("text", candidates=[_success_candidate(url)])
    p = _provider_with_response(resp)

    p.fetch_url(url, model="gemini-3.5-flash")

    kwargs = p._client.models.last_kwargs
    assert kwargs["model"] == "gemini-3.5-flash"
    assert url in kwargs["contents"]
    # A default instruction (verbatim, no summarizing) is sent when the caller supplies none.
    assert "verbatim" in kwargs["contents"].lower()
    tool = kwargs["config"].tools[0]
    assert tool.url_context is not None


def test_fetch_url_passes_through_a_custom_instruction():
    url = "https://example.com/article"
    resp = _FakeGeminiFetchResponse("text", candidates=[_success_candidate(url)])
    p = _provider_with_response(resp)

    p.fetch_url(url, model="gemini-3.5-flash", instruction="Summarize the pricing section only.")

    kwargs = p._client.models.last_kwargs
    assert "Summarize the pricing section only." in kwargs["contents"]


# --------------------------------------------------------------------------- #
# fetch_url() -- failure modes
# --------------------------------------------------------------------------- #

def test_fetch_url_raises_on_non_success_retrieval_status():
    url = "https://example.com/blocked"
    entry = _FakeUrlMetadataEntry("URL_RETRIEVAL_STATUS_ERROR", url)
    resp = _FakeGeminiFetchResponse("", candidates=[_FakeCandidate(_FakeUrlContextMetadata([entry]))])
    p = _provider_with_response(resp)

    with pytest.raises(RuntimeError, match="could not retrieve"):
        p.fetch_url(url, model="gemini-3.5-flash")


def test_fetch_url_raises_on_empty_text_even_with_success_status():
    url = "https://example.com/blank"
    resp = _FakeGeminiFetchResponse("   ", candidates=[_success_candidate(url)])
    p = _provider_with_response(resp)

    with pytest.raises(RuntimeError, match="no content"):
        p.fetch_url(url, model="gemini-3.5-flash")


def test_fetch_url_records_token_usage_when_usage_metadata_present():
    class _Usage:
        prompt_token_count = 42
        candidates_token_count = 7

    url = "https://example.com/article"
    resp = _FakeGeminiFetchResponse(
        "text", candidates=[_success_candidate(url)], usage_metadata=_Usage()
    )
    p = _provider_with_response(resp)

    p.fetch_url(url, model="gemini-3.5-flash")

    assert p.tokens_in == 42
    assert p.tokens_out == 7
