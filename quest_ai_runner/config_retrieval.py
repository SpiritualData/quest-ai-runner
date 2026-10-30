"""Retrieval from config: folding sources in, file defaults, derived capabilities, the retrieval adapter and the model registry.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .core.model_registry import ModelRegistry
from .core.adapters import RetrievalAdapter
from .config_resolvers import resolve_deep_runner, resolve_deep_runner_ladder

if TYPE_CHECKING:
    from .config import RunnerConfig


def fold_into_retrieval(cfg: RunnerConfig, adapters: List[Any]) -> None:
    """Add ``adapters`` to ``cfg.retrieval`` as members of a ``CompositeRetrievalAdapter``.

    Idempotent (an adapter already in the stack is not added twice), and the same fold-in the web
    and MCP wiring use rather than a parallel mechanism.
    """
    from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as CRA

    existing = list(cfg.retrieval.adapters) if isinstance(cfg.retrieval, CRA) else (
        [cfg.retrieval] if cfg.retrieval is not None else [])
    fresh = [a for a in adapters if a is not None and not any(a is e for e in existing)]
    if not fresh:
        return
    if cfg.retrieval is None:
        cfg.retrieval = fresh[0] if len(fresh) == 1 else CRA(fresh)
    elif isinstance(cfg.retrieval, CRA):
        cfg.retrieval = CRA(existing + fresh, max_workers=cfg.retrieval.max_workers)
    else:
        cfg.retrieval = CRA([cfg.retrieval, *fresh])


def apply_file_defaults(cfg: RunnerConfig, file_cfg: Optional[RunnerConfig]) -> RunnerConfig:
    """Fill any field ``cfg`` still holds at ``RunnerConfig``'s own dataclass default from
    ``file_cfg``'s value, in place, and return ``cfg`` — how "a config file's values lose to
    environment variables" is implemented.

    Rather than threading a parallel file-vs-env check through every line of ``_config_from_env``
    (which builds ``cfg`` from env vars): build ``cfg`` the ordinary env-driven way first, build
    ``file_cfg`` from the file (:meth:`RunnerConfig.from_file`), then call this. A field an env var
    actually set is, by definition, already different from the class default and is left alone
    here; a field NO env var touched (still at the default) takes the file's value instead, if the
    file set something different from that same default.

    Comparing field VALUES with ``==`` (not identity) is what makes this safe for the plain-data
    fields ``from_file`` can populate (str/int/float/bool/dict/list, and ``PersonaResolverConfig``
    via its own generated ``__eq__``). A field ``from_file`` never touches (an adapter, a callable,
    one of the ``_AUTO_*`` sentinels) never differs from the default here either, because
    ``file_cfg`` — itself only ever built by ``from_file`` — never set one.

    ``file_cfg=None`` (no config file was given) is a no-op, returning ``cfg`` unchanged.
    """
    from .config import RunnerConfig  # lazy: config imports this module
    if file_cfg is None:
        return cfg
    defaults = RunnerConfig()
    for f in dataclasses.fields(RunnerConfig):
        if not f.init:
            continue
        default_value = getattr(defaults, f.name)
        current_value = getattr(cfg, f.name)
        try:
            cfg_at_default = current_value == default_value
        except Exception:  # noqa: BLE001 - an exotic __eq__ must never break config building
            cfg_at_default = current_value is default_value
        if not cfg_at_default:
            continue  # an env var (or the caller) already set this field; the file never wins here
        file_value = getattr(file_cfg, f.name)
        try:
            file_differs = file_value != default_value
        except Exception:  # noqa: BLE001
            file_differs = file_value is not default_value
        if file_differs:
            setattr(cfg, f.name, file_value)
    return cfg


def _retrieval_has_web_search(retrieval) -> bool:
    """True if a provider-native web-search adapter is already in the stack."""
    if retrieval is None:
        return False
    try:
        from .adapters.provider_web_search_adapter import ProviderWebSearchAdapter as _PWSA
        from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA
    except Exception:  # noqa: BLE001
        return False
    if isinstance(retrieval, _PWSA):
        return True
    if isinstance(retrieval, _CRA):
        return any(isinstance(a, _PWSA) for a in getattr(retrieval, "adapters", []))
    return False


def derive_capabilities(cfg: RunnerConfig) -> Dict[str, bool]:
    """Derive the {web, corpus, code} capabilities the runner can HONESTLY report from its config.

    The backend's team-environment heartbeat carries what this runner can actually do, so the
    routing classifier can decide whether to route deferred work here. We read it straight off the
    wired adapters — never assert a capability we don't have:

      * ``corpus`` — a corpus/files retrieval adapter is configured (FilesAdapter, or any adapter
        bound to a ``corpus_root``). The runner can ground on the org's files/corpus.
      * ``code``   — a SubprocessGoalRunner (or any DeepRunner) is configured. The runner can do
        deep, code/goal-driven execution.
      * ``web``    — a ``WebResearch`` adapter is wired (``cfg.web_research``, the planner's own
        ``{"web": ...}``/``{"web_page": ...}`` reads), OR the deep-runner can BROWSE the live web.
        Our reference deep-runner spawns Claude Code, which ships WebSearch/WebFetch, so a
        configured SubprocessGoalRunner with the web tools allowed CAN web-research. We read this
        off the SubprocessConfig's actual tool gating (``web_enabled()``) — NOT a hardcode — so a
        consumer that pins tools without web honestly reports web:false, while the default
        (web-capable) state reports web:true.
    """
    # corpus: a FilesAdapter (or an adapter constructed over the consumer's corpus_root).
    retrieval = cfg.retrieval
    corpus = False
    if retrieval is not None:
        # FilesAdapter is the reference corpus adapter; a corpus_root also implies file grounding.
        try:
            from .adapters import FilesAdapter
            corpus = isinstance(retrieval, FilesAdapter)
        except Exception:  # noqa: BLE001 — never let capability-detection break the runner
            corpus = False
        if not corpus and cfg.corpus_root:
            corpus = True

    # code: some execution path is wired. Resolve the tri-state first (``warn=False``: this is a
    # capability PROBE — the wiring path in build_orchestrator owns the one loud warning), so an
    # unset field reports the capability the auto-built default actually provides rather than the
    # sentinel object. The LADDER is what is probed, not just the single deep runner: a consumer
    # that wired only a file_writer (fast edits, no Claude Code) really can execute.
    deep = resolve_deep_runner(cfg, warn=False)
    code = bool(resolve_deep_runner_ladder(cfg, warn=False))

    # web: a WebResearch adapter is wired directly (cfg.web_research, the fast/cheap path), OR
    # the deep-runner browses via Claude Code's WebSearch/WebFetch (reads off the actual tool
    # gating), OR a ProviderWebSearchAdapter is wired into the retrieval stack. All honest: we detect what the config actually provides.
    web = cfg.web_research is not None
    if not web and deep is not None:
        sub_cfg = getattr(deep, "cfg", None)
        web_enabled = getattr(sub_cfg, "web_enabled", None)
        if callable(web_enabled):
            try:
                web = bool(web_enabled())
            except Exception:  # noqa: BLE001 — capability detection must never break the runner
                web = False

    # If no deep-runner web capability, check the retrieval stack for a web-search adapter:
    # a provider-native ProviderWebSearchAdapter.
    if not web and retrieval is not None:
        try:
            from .adapters.provider_web_search_adapter import ProviderWebSearchAdapter as _PWSA
            from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA

            def _is_web(a) -> bool:
                return isinstance(a, _PWSA)

            if isinstance(retrieval, _CRA):
                web = any(_is_web(a) for a in getattr(retrieval, "adapters", []))
            else:
                web = _is_web(retrieval)
        except Exception:  # noqa: BLE001 — capability detection must never break the runner
            pass

    return {"web": web, "corpus": corpus, "code": code}


def get_retrieval_adapter(cfg: RunnerConfig) -> Optional[RetrievalAdapter]:
    """Get the finalized retrieval adapter, auto-enhancing with QuestRetrievalAdapter if configured.

    If Quest credentials (quest_base_url + quest_api_key) are set and retrieval is configured,
    automatically add QuestRetrievalAdapter to the stack via CompositeRetrievalAdapter.
    If only Quest credentials are set but no retrieval adapter is configured, return None
    (the caller will handle missing retrieval gracefully).

    This enables Quest-aware context retrieval automatically without requiring explicit
    configuration in each deployment.
    """
    if cfg.retrieval is None:
        return None

    # If Quest credentials are not set, return retrieval as-is
    if not cfg.quest_base_url or not cfg.quest_api_key:
        return cfg.retrieval

    # Quest is configured; add QuestRetrievalAdapter to the stack
    try:
        from quest_ai_runner.adapters import CompositeRetrievalAdapter, QuestRetrievalAdapter
        from quest_ai_runner.runner.quest_client import QuestClient

        # Create a Quest client for the retrieval adapter
        quest_client = QuestClient(
            base_url=cfg.quest_base_url,
            api_key=cfg.quest_api_key,
            team_id=cfg.team_id,
        )
        quest_adapter = QuestRetrievalAdapter(quest_client)

        # If retrieval is already a composite, add Quest to it
        if isinstance(cfg.retrieval, CompositeRetrievalAdapter):
            # Create a new composite with Quest added
            adapters = list(cfg.retrieval.adapters) + [quest_adapter]
            return CompositeRetrievalAdapter(adapters, max_workers=cfg.retrieval.max_workers)

        # Otherwise, create a composite with both the existing adapter and Quest
        return CompositeRetrievalAdapter([cfg.retrieval, quest_adapter])
    except Exception:  # noqa: BLE001 — if auto-wiring fails, return original retrieval
        return cfg.retrieval


def build_registry(cfg: RunnerConfig) -> ModelRegistry:
    if cfg.model_provider is None:
        raise ValueError("model_provider is required to build a ModelRegistry")
    return ModelRegistry(
        cfg.model_provider,
        fallback=cfg.model_fallback or None,
        providers=cfg.model_providers or None,
        provider_overrides=cfg.model_provider_overrides or None,
    )
