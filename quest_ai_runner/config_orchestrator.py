"""The wiring steps ``build_orchestrator`` runs before it constructs the brain.

Each function takes the consumer's ``RunnerConfig`` and, in place, completes what it left unset:
multi-provider routing, live web research, MCP and opt-in channel adapters folded into retrieval, and
the default guidance provider. Split out of ``config.py`` so that module keeps the config dataclass,
the file loader and the one ``build_orchestrator`` seam; the steps and their order are unchanged.
"""
from __future__ import annotations

import os
from typing import Any

from .core.model_family import route_extra_model_prefixes
from .config_retrieval import _retrieval_has_web_search, build_registry, fold_into_retrieval
from .config_resolvers import register_deepseek
from .config_support import _log


def wire_providers(cfg: Any) -> None:
    # Auto-build all available providers for multi-provider routing
    # Any model can auto-route to the right provider based on its name prefix
    # Always register all providers so models can be routed intelligently
    from .adapters import AnthropicProvider, GeminiProvider, OpenAIProvider

    all_providers = {}
    try:
        all_providers["anthropic"] = AnthropicProvider()
        _log.debug("Registered Anthropic provider")
    except Exception as e:  # noqa: BLE001
        _log.debug(f"Anthropic provider unavailable: {type(e).__name__}")
    try:
        all_providers["gemini"] = GeminiProvider()
        _log.debug("Registered Gemini provider")
    except Exception as e:  # noqa: BLE001
        _log.debug(f"Gemini provider unavailable: {type(e).__name__}")
    try:
        all_providers["openai"] = OpenAIProvider()
        _log.debug("Registered OpenAI provider")
        route_extra_model_prefixes("openai", os.environ)
    except Exception as e:  # noqa: BLE001
        _log.debug(f"OpenAI provider unavailable: {type(e).__name__}")
    # DeepSeek is opt-in: registered ONLY when its key is set, and never auto-bucketed into a tier
    # (a deployment pins QAR_MODEL_<TIER>=deepseek-... to use it), so adding the key changes no
    # existing lane's tiers.
    register_deepseek(all_providers)

    # Always update config with all available providers (overwrite any existing)
    # This ensures multi-provider routing works correctly. Only apply when the primary
    # provider is a known real type so stub/custom providers stay offline in tests.
    _real_types = (AnthropicProvider, GeminiProvider, OpenAIProvider)
    if all_providers and isinstance(cfg.model_provider, _real_types):
        cfg.model_providers = all_providers
        _log.info(f"Multi-provider routing enabled with: {list(all_providers.keys())}")
    elif not all_providers:
        _log.warning("No multi-provider setup: no providers could be initialized")

    # Wrap providers with MultiProvider for automatic intelligent routing
    # This ensures ALL provider calls (not just orchestrator calls) route correctly
    from .adapters.multi_provider import MultiProvider

    if cfg.model_provider and all_providers:
        if isinstance(cfg.model_provider, _real_types):
            original_provider = cfg.model_provider
            cfg.model_provider = MultiProvider(
                original_provider, all_providers, usage_tracker=cfg.usage_tracker)
            # Attach a registry so a quota/rate-limit error on a tier-resolved model
            # (e.g. a "quality"/"balanced" model hitting its enforced daily Gemini quota)
            # automatically steps down to the next cheaper tier instead of erroring out.
            # Built after cfg.model_provider is wrapped so the registry's own auto-bucketing
            # sees the full multi-provider model list, same as build_registry(cfg) below.
            try:
                cfg.model_provider.set_tier_registry(build_registry(cfg))
            except Exception:  # noqa: BLE001 — fallback wiring must never block startup
                _log.debug("Could not attach tier registry to MultiProvider; fallback disabled")
            _log.debug("Wrapped primary provider with MultiProvider for intelligent routing")

    # Also wrap vision_provider if configured (used for image description fallback)
    if cfg.vision_provider and all_providers:
        original_vision_provider = cfg.vision_provider
        cfg.vision_provider = MultiProvider(original_vision_provider, all_providers)
        _log.debug("Wrapped vision provider with MultiProvider for intelligent routing")


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
