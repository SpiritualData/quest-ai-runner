"""The wiring steps ``build_orchestrator`` runs before it constructs the brain.

Each function takes the consumer's ``RunnerConfig`` and, in place, completes what it left unset:
multi-provider routing, live web research, MCP and opt-in channel adapters folded into retrieval, and
the default guidance provider. Split out of ``config.py`` so that module keeps the config dataclass,
the file loader and the one ``build_orchestrator`` seam; the steps and their order are unchanged.
"""
from __future__ import annotations

import os
from typing import Any

from hubofhubs.models.anthropic import AnthropicProvider
from hubofhubs.models.gemini import GeminiProvider
from hubofhubs.models.openai import OpenAIProvider

from .providers import make_provider, routed, route_extra_model_prefixes
from .config_retrieval import _retrieval_has_web_search, build_registry, fold_into_retrieval
from .config_resolvers import register_deepseek
from .config_support import _log


def wire_providers(cfg: Any) -> None:
    """Register the vendors as providers and wrap the primary (and the vision provider) in the hub's router, which sends each call to
    the provider that serves its model id by prefix. DeepSeek is opt-in: registered only when its key is set, and never bucketed into
    a tier (a deployment pins QAR_MODEL_<TIER>=deepseek-... to use it)."""
    all_providers = {name: make_provider(name) for name in ("anthropic", "gemini", "openai")}
    route_extra_model_prefixes("openai", os.environ)
    register_deepseek(all_providers)
    # Only when the primary is a known real vendor provider, so stub and custom providers stay offline in tests.
    real_types = (AnthropicProvider, GeminiProvider, OpenAIProvider)
    if isinstance(cfg.model_provider, real_types):
        cfg.model_providers = all_providers
        _log.info(f"Multi-provider routing enabled with: {list(all_providers.keys())}")
        original_provider = cfg.model_provider
        cfg.model_provider = routed(original_provider, all_providers, cfg.usage_tracker)
        # A quota or rate-limit error on a tier-resolved model steps down to the next cheaper tier; the registry is built after the
        # wrap so it sees the full multi-provider model list.
        try:
            cfg.model_provider.registry = build_registry(cfg)
        except Exception:  # noqa: BLE001 - fallback wiring must never block startup
            _log.debug("Could not attach tier registry to MultiProvider; fallback disabled")
    if cfg.vision_provider:
        cfg.vision_provider = routed(cfg.vision_provider, all_providers)


def wire_web_research(cfg: Any) -> None:
    # Fast, token-efficient LIVE WEB access (``core.adapters_retrieval.WebResearch``): built once here from
    # env (backend auto-selection, snippet-first search, bounded page fetch, cache; see
    # ``adapters/web_research.py``), reached by the planner through its OWN read keys
    # (``{"web": ...}``, ``{"web_page": ...}``) — never folded into ``cfg.retrieval``/
    # ``CompositeRetrievalAdapter``, because that composite BROADCASTS every grep/query to every
    # member adapter, which would fire a paid, slow web search on an ordinary corpus grep or DB
    # query. A consumer that already set ``cfg.web_research`` (its own adapter, or a test fake) is
    # never overwritten.
    _web_disabled = (os.getenv("WEB_SEARCH_ENABLED") or "").strip().lower() in ("false", "0", "off", "no")
    if cfg.web_research is None and not _web_disabled:
        try:
            from .adapters.web_research import build_web_research_from_env
            cfg.web_research = build_web_research_from_env(os.environ, provider=cfg.model_provider)
            if cfg.web_research is not None:
                _log.info("Web research adapter wired: %s", cfg.web_research.describe())
        except ImportError as e:
            _log.debug("web_research adapter not available (not installed/importable): %s", e)
        except Exception as e:  # noqa: BLE001 — web research is optional; never break the build
            _log.debug("web_research not wired: %s", e)

    # LEGACY fold-in, reached ONLY when the fast path above left ``cfg.web_research`` unset (the
    # ``adapters.web_research`` module is missing, or it found no usable backend) — so a
    # deployment without the newer module never regresses to no web capability at all. Unless
    # web search is explicitly disabled (WEB_SEARCH_ENABLED=false), wire a
    # ProviderWebSearchAdapter when the model provider supports NATIVE web search (Anthropic
    # web_search tool / Gemini Google Search grounding) — reusing the LLM key, so no separate
    # web-search key is needed.
    if cfg.web_research is None and not _web_disabled and not _retrieval_has_web_search(cfg.retrieval):
        try:
            provider = cfg.model_provider
            supports = getattr(provider, "supports_web_search", None)
            if provider is not None and callable(supports):
                from .core.model_registry import ModelRegistry as _WReg
                _wreg = _WReg(provider, fallback=cfg.model_fallback or None)
                web_tier = (os.getenv("WEB_SEARCH_TIER") or "balanced").strip() or "balanced"
                web_model = _wreg.resolve_tier(web_tier)
                if web_model and supports(web_model):
                    from .adapters.provider_web_search_adapter import ProviderWebSearchAdapter
                    from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA
                    try:
                        _wmax = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
                    except ValueError:
                        _wmax = 5
                    native_web = ProviderWebSearchAdapter(provider, model=web_model, max_results=_wmax)
                    if cfg.retrieval is None:
                        cfg.retrieval = native_web
                    elif isinstance(cfg.retrieval, _CRA):
                        cfg.retrieval = _CRA(
                            list(cfg.retrieval.adapters) + [native_web],
                            max_workers=cfg.retrieval.max_workers,
                        )
                    else:
                        cfg.retrieval = _CRA([cfg.retrieval, native_web])
                    _log.info("Native web search enabled (legacy fold-in; %s, model=%s)",
                              type(provider).__name__, web_model)
        except Exception as e:  # noqa: BLE001 — web search is optional; never break the build
            _log.debug("native web search not wired: %s", e)


def wire_mcp_and_channels(cfg: Any) -> None:
    # Generic MCP (Model Context Protocol) servers -- fold each configured MCPServerSpec into the
    # retrieval stack as its own namespaced MCPRetrievalAdapter (read-only foundation phase; see
    # adapters/mcp_client.py + adapters/mcp_retrieval_adapter.py). Same fold-into-composite pattern
    # as the native web-search wiring just above, not a parallel mechanism.
    if cfg.mcp_servers:
        try:
            from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA
            from .adapters.mcp_retrieval_adapter import MCPRetrievalAdapter as _MCPRA

            mcp_adapters = [
                _MCPRA(alias=spec.alias, spec=spec, allowed_tools=spec.allowed_tools)
                for spec in cfg.mcp_servers
            ]
            if cfg.retrieval is None:
                cfg.retrieval = mcp_adapters[0] if len(mcp_adapters) == 1 else _CRA(mcp_adapters)
            elif isinstance(cfg.retrieval, _CRA):
                cfg.retrieval = _CRA(
                    list(cfg.retrieval.adapters) + mcp_adapters, max_workers=cfg.retrieval.max_workers,
                )
            else:
                cfg.retrieval = _CRA([cfg.retrieval, *mcp_adapters])
            _log.info("MCP servers wired into retrieval: %s", [s.alias for s in cfg.mcp_servers])
        except Exception as e:  # noqa: BLE001 — MCP wiring is optional; never break the build
            _log.debug("mcp servers not wired: %s", e)

    # Notion and Google Chat (built by resolve_config_objects, or supplied live): the SAME objects the
    # context-update engine reads, folded into retrieval so a run can read, grep and query them.
    # Both are read-only adapters and only ever present when explicitly configured.
    _opt_in_channels = [a for a in (cfg.notion_adapter, cfg.google_chat_adapter) if a is not None]
    if _opt_in_channels:
        try:
            fold_into_retrieval(cfg, _opt_in_channels)
            _log.info("Opt-in channels wired into retrieval: %s",
                      [type(a).__name__ for a in _opt_in_channels])
        except Exception as e:  # noqa: BLE001 -- optional wiring never breaks the build
            _log.debug("opt-in channels not wired: %s", e)


def default_guidance(cfg: Any) -> Any:
    """The guidance provider to use when ``cfg.guidance_provider`` is unset."""
    # Auto-enable guidance provider if not configured
    guidance = cfg.guidance_provider
    if guidance is None:
        from quest_ai_runner.core.guidance_provider import UniversalGuidanceProvider
        from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager
        from quest_ai_runner.adapters.quest_guidance_loader import QuestGuidanceLoader
        from quest_ai_runner.runner.quest_client import QuestClient

        # If Quest is configured, load dynamic guidance from Quest backend
        dynamic_loader = None
        if cfg.quest_base_url and cfg.quest_api_key:
            try:
                quest_client = QuestClient(
                    base_url=cfg.quest_base_url,
                    api_key=cfg.quest_api_key,
                    team_id=cfg.team_id,
                )
                dynamic_loader = QuestGuidanceLoader(quest_client, team_id=cfg.team_id)
            except Exception:  # noqa: BLE001
                pass  # Quest loader optional; guidance works without it

        from .core.model_registry import ModelRegistry as _GuidanceRegistry
        _g_registry = _GuidanceRegistry(cfg.model_provider, fallback=cfg.model_fallback or None)
        guidance = UniversalGuidanceProvider(
            dynamic_guidance_loader=dynamic_loader,
            provider=cfg.model_provider,
            model=_g_registry.resolve_tier("balanced"),
            manager_factory=GuidanceCardManager,
        )

    return guidance
