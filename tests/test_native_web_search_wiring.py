"""build_orchestrator wires provider-native web search by default (no extra key needed).

Verifies the key-free default: when the model provider reports supports_web_search(), a
ProviderWebSearchAdapter is added to the retrieval stack automatically, unless
WEB_SEARCH_ENABLED=false opts out. Also checks derive_capabilities reports web=True.
"""
from __future__ import annotations

from tests.conftest import StubProvider, StubRetrieval, StubEscalation
from quest_ai_runner.config import RunnerConfig, build_orchestrator, derive_capabilities
from quest_ai_runner.adapters import ProviderWebSearchAdapter, CompositeRetrievalAdapter


class WebStubProvider(StubProvider):
    """A stub provider that advertises native web search."""

    def supports_web_search(self, model=None) -> bool:
        return True

    def web_search(self, query, *, model, max_results=5):
        return {"answer": "A", "results": [{"title": "T", "url": "https://x", "snippet": ""}]}


class NoWebStubProvider(StubProvider):
    """A stub provider with NO native web search (the default base behavior)."""


def _has_native(retrieval) -> bool:
    if isinstance(retrieval, ProviderWebSearchAdapter):
        return True
    if isinstance(retrieval, CompositeRetrievalAdapter):
        return any(isinstance(a, ProviderWebSearchAdapter) for a in retrieval.adapters)
    return False


def _cfg(provider):
    return RunnerConfig(
        retrieval=StubRetrieval({"README.md": "hi"}),
        model_provider=provider,
        model_fallback={"balanced": "gemini-3.5-flash"},
        escalation=StubEscalation(),
    )


def test_native_web_search_wired_by_default(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    cfg = _cfg(WebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert _has_native(orch.retrieval)
    assert derive_capabilities(cfg)["web"] is True


def test_web_search_enabled_false_opts_out(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    cfg = _cfg(WebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert not _has_native(orch.retrieval)


def test_provider_without_web_search_is_not_wired(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    cfg = _cfg(NoWebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert not _has_native(orch.retrieval)
    assert derive_capabilities(cfg)["web"] is False
