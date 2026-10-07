"""Fast, token-efficient LIVE WEB reads (part B of the feature; part A is
``adapters/web_research.py`` and friends).

This covers the GENERIC wiring and dispatch on the ``core``/``config`` side: the ``WebResearch``
read keys (``{"web": ...}``, ``{"web_page": ...}``), the planner's WEB block and decide-schema
fields (opt-in, zero cost when unconfigured), status/EVENT_READ surfacing, spec-kind
classification, the reach judge's web-aware "world" wording, and ``build_orchestrator`` wiring
``cfg.web_research`` instead of folding a web-search adapter into the retrieval composite.

Fully offline: a FAKE ``WebResearch`` object (matching the interface in
``core/adapters.WebResearch`` / ``adapters/web_research.WebResearchAdapter``), never the real
backends (those are part A's own tests: ``test_web_search_backends.py``,
``test_web_cache.py``, ``test_web_page_extract.py``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from quest_ai_runner.adapters import ProviderWebSearchAdapter, CompositeRetrievalAdapter
from quest_ai_runner.config import RunnerConfig, build_orchestrator
from quest_ai_runner.core.adapters import Observation, StreamSink
from quest_ai_runner.core.answer_explanation import TurnTrace
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    EVENT_READ,
    Orchestrator,
    OrchestratorConfig,
    _DISCOVERY_SPEC_KEYS,
    _is_discovery_spec,
    decide_tool_for,
    describe_read_spec,
    normalize_decision,
    planner_web_block,
)
from quest_ai_runner.core.reach_judge import verdict_block, WORLD_LINE, WORLD_LINE_WEB
from quest_ai_runner.core.sufficiency import READ_SPEC_KEYS

from .conftest import StubEscalation, StubProvider, StubRetrieval


class FakeWeb:
    """A ``WebResearch`` adapter, matching the interface part A implements for real."""

    backend_name = "fake"

    def __init__(self, *, raise_on_search: bool = False, raise_on_fetch: bool = False):
        self.search_calls: List[Dict[str, Any]] = []
        self.fetch_calls: List[Dict[str, Any]] = []
        self._raise_on_search = raise_on_search
        self._raise_on_fetch = raise_on_fetch

    def search(self, queries: Union[str, List[str]], *, max_results: Optional[int] = None,
              fresh: bool = False) -> Observation:
        self.search_calls.append({"queries": queries, "max_results": max_results, "fresh": fresh})
        if self._raise_on_search:
            raise RuntimeError("backend unreachable")
        qs = queries if isinstance(queries, str) else ", ".join(queries)
        return Observation(
            kind="query", rel_path=f"web_search:{qs}",
            text=f'WEB RESULTS for "{qs}" (cite as [title](url)):\n1. Example - https://x.example/1',
            hits=[{"title": "Example", "url": "https://x.example/1", "snippet": "a snippet",
                   "query": qs, "source": "fake"}],
        )

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False) -> Observation:
        self.fetch_calls.append({"url": url, "focus": focus, "fresh": fresh})
        if self._raise_on_fetch:
            raise RuntimeError("fetch failed")
        return Observation(kind="read", rel_path=url, locator=f"web extract: {url}",
                           text=f"Relevant passage about {focus or 'the page'}.")

    def describe(self) -> str:
        return "web search (fake)"


def _orch(provider, *, web=None, tools=None, config=None) -> Orchestrator:
    cfg = config or OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, web=web, tools=tools)


# ---------------------------------------------------------------------------------------------
# dispatch: _exec_one_read
# ---------------------------------------------------------------------------------------------

def test_web_search_dispatch_single_query():
    web = FakeWeb()
    orch = _orch(StubProvider([]), web=web)
    obs = orch._exec_one_read({"web": "tide tables bristol 2026"})
    assert web.search_calls == [{"queries": "tide tables bristol 2026", "max_results": None,
                                 "fresh": False}]
    assert obs.kind == "query"
    assert obs.rel_path == "web_search:tide tables bristol 2026"
    assert obs.hits and obs.hits[0]["url"] == "https://x.example/1"


def test_web_search_dispatch_list_of_queries_runs_as_one_call():
    web = FakeWeb()
    orch = _orch(StubProvider([]), web=web)
    obs = orch._exec_one_read({"web": ["query one", "query two"], "fresh": True})
    assert web.search_calls == [{"queries": ["query one", "query two"], "max_results": None,
                                 "fresh": True}]
    assert obs.kind == "query"


def test_web_page_dispatch_with_focus():
    web = FakeWeb()
    orch = _orch(StubProvider([]), web=web)
    obs = orch._exec_one_read({"web_page": "https://x.example/1", "focus": "pricing"})
    assert web.fetch_calls == [{"url": "https://x.example/1", "focus": "pricing", "fresh": False}]
    assert obs.kind == "read"
    assert obs.rel_path == "https://x.example/1"
    assert obs.locator == "web extract: https://x.example/1"


def test_web_page_dispatch_fresh_flag():
    web = FakeWeb()
    orch = _orch(StubProvider([]), web=web)
    orch._exec_one_read({"web_page": "https://x.example/1", "fresh": True})
    assert web.fetch_calls[0]["fresh"] is True


def test_web_unconfigured_returns_named_error_never_raises():
    orch = _orch(StubProvider([]), web=None)
    obs = orch._exec_one_read({"web": "anything"})
    assert obs.kind == "error"
    assert "not configured" in obs.error

    obs2 = orch._exec_one_read({"web_page": "https://x.example"})
    assert obs2.kind == "error"
    assert "not configured" in obs2.error


def test_web_adapter_exception_degrades_to_error_observation_not_a_raise():
    web = FakeWeb(raise_on_search=True, raise_on_fetch=True)
    orch = _orch(StubProvider([]), web=web)
    obs = orch._exec_one_read({"web": "q"})
    assert obs.kind == "error"
    assert "backend unreachable" in obs.error

    obs2 = orch._exec_one_read({"web_page": "https://x.example"})
    assert obs2.kind == "error"
    assert "fetch failed" in obs2.error


# ---------------------------------------------------------------------------------------------
# spec-kind classification: web/web_page are REAL content reads, not discovery
# ---------------------------------------------------------------------------------------------

def test_web_specs_are_not_discovery():
    assert "web" not in _DISCOVERY_SPEC_KEYS
    assert "web_page" not in _DISCOVERY_SPEC_KEYS
    assert _is_discovery_spec({"web": "q"}) is False
    assert _is_discovery_spec({"web_page": "https://x"}) is False


def test_web_specs_count_as_read_specs_for_the_sufficiency_gate():
    assert "web" in READ_SPEC_KEYS
    assert "web_page" in READ_SPEC_KEYS


def test_describe_read_spec_names_web_reads():
    assert describe_read_spec({"web": "the query"}) == "web('the query')"
    assert describe_read_spec({"web_page": "https://x.example"}) == "web_page('https://x.example')"


# ---------------------------------------------------------------------------------------------
# normalize_decision: the clean_reads filter keeps web/web_page ONLY when enabled
# ---------------------------------------------------------------------------------------------

def test_normalize_decision_keeps_web_reads_only_when_web_enabled():
    cfg = OrchestratorConfig()
    raw = {"action": "read", "rationale": "look it up",
          "reads": [{"web": "today's news"}, {"web_page": "https://x.example"}]}
    # web not enabled: both specs are dropped, so there is nothing to read -> falls back cleanly.
    decision_off = normalize_decision(raw, cfg, web_enabled=False)
    assert decision_off.reads == []
    # web enabled: both specs survive the filter untouched.
    decision_on = normalize_decision(raw, cfg, web_enabled=True)
    assert decision_on.reads == raw["reads"]


# ---------------------------------------------------------------------------------------------
# decide-tool schema: web/web_page/focus/fresh appear ONLY when configured
# ---------------------------------------------------------------------------------------------

def test_decide_schema_web_fields_only_when_configured():
    plain = decide_tool_for(False, False)
    read_props = plain["input_schema"]["properties"]["reads"]["items"]["properties"]
    assert "web" not in read_props
    assert "web_page" not in read_props
    assert "focus" not in read_props
    assert "fresh" not in read_props

    with_web = decide_tool_for(False, False, web=True)
    read_props_web = with_web["input_schema"]["properties"]["reads"]["items"]["properties"]
    assert {"web", "web_page", "focus", "fresh"} <= set(read_props_web)

    # the shared base schema is never mutated by the web=True branch
    assert "web" not in decide_tool_for(False, False)["input_schema"]["properties"]["reads"][
        "items"]["properties"]


def test_decide_schema_identity_preserved_when_web_not_requested():
    """decide_tool_for(False, False) with web defaulting to False must still return the shared
    DECIDE_TOOL singleton (identity, not just equality) -- existing callers rely on this."""
    from quest_ai_runner.core import orchestrator as orch_mod
    assert decide_tool_for(False, False) is orch_mod.DECIDE_TOOL


# ---------------------------------------------------------------------------------------------
# the planner prompt: byte-for-byte unchanged with no web adapter, WEB block after the body
# ---------------------------------------------------------------------------------------------

def test_prompt_byte_identical_without_web_and_web_block_added_after_body_when_configured():
    message = "What's the latest news on this?"
    decisions = [{"action": "answer", "rationale": "ok"}]

    provider_no_web = StubProvider(decisions=list(decisions))
    _orch(provider_no_web, web=None).run(message, quest_id="quest_1")
    prompt_no_web = provider_no_web.plan_prompts[0]
    assert "LIVE WEB" not in prompt_no_web

    web = FakeWeb()
    provider_web = StubProvider(decisions=list(decisions))
    orch_web = _orch(provider_web, web=web)
    orch_web.run(message, quest_id="quest_1")
    prompt_web = provider_web.plan_prompts[0]

    assert "LIVE WEB" in prompt_web
    assert prompt_web.index("LIVE WEB") > prompt_web.index(message)

    # Byte-for-byte: the web-configured prompt is EXACTLY the unconfigured prompt plus the WEB
    # block appended after it -- nothing else about the body changed.
    web_block = planner_web_block(web)
    assert web_block
    assert prompt_web == prompt_no_web + "\n\n" + web_block


# ---------------------------------------------------------------------------------------------
# status text + EVENT_READ sources
# ---------------------------------------------------------------------------------------------

def test_status_text_for_web_search_and_web_page():
    statuses: List[str] = []
    web = FakeWeb()
    provider = StubProvider(decisions=[
        {"action": "read", "rationale": "search", "reads": [{"web": "today's weather"}]},
        {"action": "answer", "rationale": "done"},
    ])
    cfg = OrchestratorConfig(overseer=False)
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, web=web,
                        status=statuses.append)
    orch.run("what's today's weather", quest_id="quest_1")
    assert "Searching the web…" in statuses

    statuses.clear()
    provider2 = StubProvider(decisions=[
        {"action": "read", "rationale": "fetch", "reads": [{"web_page": "https://x.example/1"}]},
        {"action": "answer", "rationale": "done"},
    ])
    orch2 = Orchestrator(retrieval=StubRetrieval(), provider=provider2,
                         registry=ModelRegistry(provider2), config=cfg, web=FakeWeb(),
                         status=statuses.append)
    orch2.run("read that page", quest_id="quest_1")
    assert "Reading a web page…" in statuses


def test_event_read_sources_are_result_urls_for_web_search_and_page_url_for_fetch():
    events: List[Dict[str, Any]] = []
    web = FakeWeb()
    provider = StubProvider(decisions=[
        {"action": "read", "rationale": "search", "reads": [{"web": "today's weather"}]},
        {"action": "answer", "rationale": "done"},
    ])
    cfg = OrchestratorConfig(overseer=False)
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, web=web)
    sink = StreamSink(events.append)
    orch.run("what's today's weather", quest_id="quest_1", sink=sink)
    read_events = [e for e in events if e.get("type") == EVENT_READ]
    assert read_events
    assert read_events[0]["data"]["sources"] == ["https://x.example/1"]

    events.clear()
    provider2 = StubProvider(decisions=[
        {"action": "read", "rationale": "fetch", "reads": [{"web_page": "https://x.example/1"}]},
        {"action": "answer", "rationale": "done"},
    ])
    orch2 = Orchestrator(retrieval=StubRetrieval(), provider=provider2,
                         registry=ModelRegistry(provider2), config=cfg, web=FakeWeb())
    sink2 = StreamSink(events.append)
    orch2.run("read that page", quest_id="quest_1", sink=sink2)
    read_events2 = [e for e in events if e.get("type") == EVENT_READ]
    assert read_events2
    assert read_events2[0]["data"]["sources"] == ["https://x.example/1"]


# ---------------------------------------------------------------------------------------------
# answer_explanation.used_web() still keys on the SAME rel_path/locator shapes
# ---------------------------------------------------------------------------------------------

def test_used_web_detects_web_search_and_web_page_observations():
    search_obs = Observation(kind="query", rel_path="web_search:today's news").to_dict()
    page_obs = Observation(kind="read", rel_path="https://x.example/1",
                           locator="web extract: https://x.example/1").to_dict()
    assert TurnTrace(gathered=[search_obs]).used_web() is True
    assert TurnTrace(gathered=[page_obs]).used_web() is True
    assert TurnTrace(gathered=[{"kind": "read", "rel_path": "docs/readme.md"}]).used_web() is False


# ---------------------------------------------------------------------------------------------
# reach judge: the "world" verdict text depends on whether web is configured, never a keyword
# check on model output -- a structural flag passed in by the caller.
# ---------------------------------------------------------------------------------------------

def test_verdict_block_world_text_depends_on_web_configured_flag():
    verdict = {"reach": "world", "covered_by": None}
    assert verdict_block(verdict) == verdict_block(verdict, web_configured=False)
    assert WORLD_LINE in verdict_block(verdict, web_configured=False)
    assert WORLD_LINE_WEB in verdict_block(verdict, web_configured=True)
    assert "{\"web\":" in verdict_block(verdict, web_configured=True)
    # "outside"/"inside" verdicts are unaffected by the flag.
    inside = {"reach": "inside", "covered_by": None}
    assert verdict_block(inside, web_configured=True) == verdict_block(inside, web_configured=False) == ""


# ---------------------------------------------------------------------------------------------
# build_orchestrator: wires cfg.web_research, never broadcasts it through the retrieval
# composite, and the legacy provider-native fold-in is reached only as a fallback.
# ---------------------------------------------------------------------------------------------

class _WebCapableProvider(StubProvider):
    """A model provider that supports native web search (would trigger the LEGACY fold-in if
    ``cfg.web_research`` were left unset and nothing else beat it to the punch)."""

    def supports_web_search(self, model=None) -> bool:
        return True

    def web_search(self, query, *, model, max_results=5):
        return {"answer": "A", "results": [{"title": "T", "url": "https://x", "snippet": ""}]}


def _runner_cfg(**overrides) -> RunnerConfig:
    base = dict(
        retrieval=StubRetrieval({"README.md": "hi"}),
        model_provider=_WebCapableProvider([]),
        model_fallback={"balanced": "gemini-3.5-flash"},
        escalation=StubEscalation(),
        deep_runner=None,
    )
    base.update(overrides)
    return RunnerConfig(**base)


def _has_legacy_native_fold_in(retrieval) -> bool:
    if isinstance(retrieval, ProviderWebSearchAdapter):
        return True
    if isinstance(retrieval, CompositeRetrievalAdapter):
        return any(isinstance(a, ProviderWebSearchAdapter) for a in retrieval.adapters)
    return False


def test_build_orchestrator_never_broadcasts_web_research_through_the_composite(monkeypatch):
    for name in ("QAR_WEB_SEARCH_BACKEND", "SERPER_API_KEY", "BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY", "TAVILY_API_KEY", "WEB_SEARCH_API_KEY", "SEARXNG_URL",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY",
                "QAR_WEB_SEARCH_PROVIDER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    web = FakeWeb()
    retrieval = StubRetrieval({"README.md": "hi"})
    cfg = _runner_cfg(retrieval=retrieval, web_research=web)
    orch = build_orchestrator(cfg)
    assert orch.web is web
    # retrieval is untouched: no composite, no web adapter folded in anywhere near it.
    assert orch.retrieval is retrieval
    orch.retrieval.grep("hi")
    assert web.search_calls == []
    assert not _has_legacy_native_fold_in(orch.retrieval)


def test_build_orchestrator_wires_web_research_from_env_and_skips_legacy_fold_in(monkeypatch):
    for name in ("QAR_WEB_SEARCH_BACKEND", "SERPER_API_KEY", "BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY", "TAVILY_API_KEY", "WEB_SEARCH_API_KEY", "SEARXNG_URL",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY",
                "QAR_WEB_SEARCH_PROVIDER_MODEL", "WEB_SEARCH_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    built = FakeWeb()

    def fake_builder(env=None, *, provider=None):
        return built

    monkeypatch.setattr(
        "quest_ai_runner.adapters.web_research.build_web_research_from_env", fake_builder)
    cfg = _runner_cfg()
    orch = build_orchestrator(cfg)
    assert cfg.web_research is built
    assert orch.web is built
    # The provider DOES support native web search, so if the legacy fold-in had ALSO run (a
    # regression: both paths firing) it would show up here. It must not, since cfg.web_research
    # was already set by the fast path above.
    assert not _has_legacy_native_fold_in(orch.retrieval)


def test_build_orchestrator_respects_an_already_set_web_research(monkeypatch):
    """A consumer that wires its own cfg.web_research is never overwritten."""
    mine = FakeWeb()

    def fake_builder(env=None, *, provider=None):
        raise AssertionError("build_web_research_from_env must not be called when already set")

    monkeypatch.setattr(
        "quest_ai_runner.adapters.web_research.build_web_research_from_env", fake_builder)
    cfg = _runner_cfg(web_research=mine)
    orch = build_orchestrator(cfg)
    assert orch.web is mine


def test_web_search_enabled_false_leaves_web_research_unset_and_skips_legacy_too(monkeypatch):
    for name in ("QAR_WEB_SEARCH_BACKEND", "SERPER_API_KEY", "BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY", "TAVILY_API_KEY", "WEB_SEARCH_API_KEY", "SEARXNG_URL",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY",
                "QAR_WEB_SEARCH_PROVIDER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    cfg = _runner_cfg()
    orch = build_orchestrator(cfg)
    assert cfg.web_research is None
    assert orch.web is None
    assert not _has_legacy_native_fold_in(orch.retrieval)
