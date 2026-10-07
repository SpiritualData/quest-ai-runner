"""build_orchestrator's LEGACY provider-native web search fold-in (no extra key needed).

Historically this was THE default: when the model provider reported supports_web_search(), a
ProviderWebSearchAdapter was folded into the retrieval stack automatically. Since the fast
WebResearch path landed (``cfg.web_research``, ``adapters/web_research.py``), that composite
fold-in is only a FALLBACK, reached only when ``build_web_research_from_env`` could not build an
adapter (no backend key/config present at all). These tests isolate every backend-selection env
var (search-provider keys, Gemini keys, QAR_WEB_SEARCH_BACKEND/_PROVIDER_MODEL) so the fast path
deterministically finds nothing and the legacy fold-in this file is actually about gets exercised
-- without this isolation, a host environment carrying a real Gemini/OpenAI key (common on this
org's dev boxes) makes ``cfg.web_research`` build successfully via the "gemini" backend and the
legacy path never runs, which is a host-environment leak, not a real failure. See
tests/test_web_reads.py for the NEW cfg.web_research wiring/dispatch behavior.
"""
from __future__ import annotations

from tests.conftest import StubProvider, StubRetrieval, StubEscalation
from quest_ai_runner.config import RunnerConfig, build_orchestrator, derive_capabilities
from quest_ai_runner.adapters import ProviderWebSearchAdapter, CompositeRetrievalAdapter

#: Every env var ``select_search_backend``/``build_web_research_from_env`` reads to decide a
#: backend is available. Cleared in every test here so an ambient host key (e.g. a real
#: GOOGLE_API_KEY set for this org's own Gemini usage) can never make the FAST path succeed and
#: shadow the LEGACY fold-in this file tests.
_WEB_BACKEND_ENV_VARS = (
    "QAR_WEB_SEARCH_BACKEND", "QAR_WEB_SEARCH_PROVIDER_MODEL", "QAR_WEB_SEARCH_MODEL",
    "SERPER_API_KEY", "BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY",
    "TAVILY_API_KEY", "WEB_SEARCH_API_KEY", "SEARXNG_URL",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY",
)


def _isolate_web_backend_env(monkeypatch) -> None:
    for name in _WEB_BACKEND_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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
        # Explicitly DISABLED, not merely unset: these tests are about the PROVIDER's web
        # capability, and a deep runner is the other thing that can make web=True (Claude Code
        # ships WebSearch/WebFetch). Leaving the field unset now means "auto-build the default
        # runner", which would make web=True for a reason this file is not testing.
        deep_runner=None,
    )


def test_native_web_search_wired_by_default(monkeypatch):
    _isolate_web_backend_env(monkeypatch)
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    cfg = _cfg(WebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert _has_native(orch.retrieval)
    assert derive_capabilities(cfg)["web"] is True


def test_web_search_enabled_false_opts_out(monkeypatch):
    _isolate_web_backend_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    cfg = _cfg(WebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert not _has_native(orch.retrieval)
    assert cfg.web_research is None


def test_provider_without_web_search_is_not_wired(monkeypatch):
    _isolate_web_backend_env(monkeypatch)
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    cfg = _cfg(NoWebStubProvider([]))
    orch = build_orchestrator(cfg)
    assert not _has_native(orch.retrieval)
    assert derive_capabilities(cfg)["web"] is False
