"""Resolving optional pieces from config: deep runners and ladders, reference and rep sync, MCP writers, recent context, anticipation, tools.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, List

from esoi import implementation

from .core.model_registry import ModelRegistry
from .providers import make_provider
from .config_support import _AUTO_DEEP_RUNNER, _log

if TYPE_CHECKING:
    from .config import RunnerConfig


def resolve_deep_runner(cfg: RunnerConfig, *, warn: bool = True):
    """Resolve the deep (execution) worker from config — ON BY DEFAULT.

    Tri-state on ``cfg.deep_runner``, mirroring ``resolve_context_assembler``:
      * ``_AUTO_DEEP_RUNNER`` (the field default, i.e. the consumer left it unset) → build the
        default ``SubprocessGoalRunner`` (Claude Code headless) so execution works out of the box.
      * an INSTANCE → use it as-is (``AcpDeepRunner``, a queue worker, a test double, …).
      * ``None`` → deep execution is explicitly DISABLED. No warning: that is a deliberate choice.

    The auto-built runner reuses EXACTLY the env vars the rest of the repo already documents for
    the deep worker, so there is one set of knobs, not two:
      * ``QAR_DEEP_WORKING_DIR`` → the subprocess cwd, falling back to ``cfg.corpus_root`` and then
        the process cwd (same precedence the CLI has always used).
      * ``QAR_CLAUDE_PATH`` → the worker binary (default ``claude``, looked up on PATH).
      * ``QAR_DEEP_TIMEOUT_SECONDS`` → applied inside the runner itself
        (``goal_runner.resolve_deep_timeout_seconds``); nothing to pass here.
      * ``QAR_DEEP_MODELS`` → the goal loop's model ladder, which lives on ``OrchestratorConfig``,
        not on the runner.

    ``cfg.context_preamble``, when set, becomes the auto-built worker's ``context_preamble`` — the
    one field a consumer most often wants to change is now reachable without building a
    ``SubprocessConfig`` by hand (see the field's own docstring on ``RunnerConfig``).

    Degrades gracefully and NEVER raises. If the worker binary is not on PATH we return ``None``
    (no runner) after logging a loud warning: building a runner that would fail on every spawn
    would be worse than reporting honestly that execution is unavailable. ``warn=False`` silences
    that warning for callers that are only probing capability (``derive_capabilities``) rather than
    actually wiring the brain.
    """
    chosen = getattr(cfg, "deep_runner", None)
    if chosen is not _AUTO_DEEP_RUNNER:
        return chosen  # an explicit instance, or None to disable
    try:
        import shutil

        import dataclasses
        import inspect

        from .adapters.claude_cli_provider import cli_model
        from .core.resolve import chosen_name, resolve, resolve_config

        # Which runner and its config class come from the declared implementations (core/resolve.py),
        # by ``cfg.selections["run-deep-work"]``; the default is the SubprocessGoalRunner.
        sel = getattr(cfg, "selections", None)
        name = chosen_name("run-deep-work", overrides=sel)
        runner_cls, config_cls = resolve("run-deep-work", overrides=sel), resolve_config("run-deep-work", overrides=sel)
        if config_cls is None:
            raise ValueError(f"run-deep-work implementation {name!r} is a ladder rung, not a standalone deep worker")
        claude_path = os.getenv("QAR_CLAUDE_PATH", "claude") or "claude"
        field_names = {f.name for f in dataclasses.fields(config_cls)}
        found = claude_path
        if "claude_path" in field_names:
            # A bare name has to be findable on PATH; an explicit path has to exist and be executable.
            if os.path.sep in claude_path:
                found = claude_path if os.access(claude_path, os.X_OK) else None
            else:
                found = shutil.which(claude_path)
        if not found:
            if warn:
                _log.warning(
                    "deep runner: no %r binary found on PATH, so DEEP/EXECUTION work is "
                    "UNAVAILABLE for this process (requests that need real work will be planned "
                    "but not carried out). Install Claude Code, set QAR_CLAUDE_PATH to the "
                    "binary, or wire RunnerConfig.deep_runner explicitly. Pass "
                    "deep_runner=None to disable execution deliberately and silence this.",
                    claude_path,
                )
            return None
        working_dir = (os.getenv("QAR_DEEP_WORKING_DIR")
                       or cfg.corpus_root
                       or os.getcwd())
        wanted = dict(working_dir=working_dir, claude_path=claude_path, context_preamble=cfg.context_preamble or "")
        config = config_cls(**{k: v for k, v in wanted.items() if k in field_names})
        extra = {}
        if "model_translator" in inspect.signature(runner_cls.__init__).parameters:
            extra["model_translator"] = cli_model   # core cannot import the adapter, so config injects it
        runner = runner_cls(config, **extra)
        _log.info("deep runner: using the default %s (%s) (%s, cwd=%s)", runner_cls.__name__, name, found, working_dir)
        return runner
    except Exception:  # noqa: BLE001 — a failed default must never stop the runner from starting
        if warn:
            _log.warning("deep runner: building the default SubprocessGoalRunner failed; deep/"
                         "execution work will be unavailable for this process", exc_info=True)
        return None


def resolve_rep_sync_resolver(cfg: RunnerConfig, *, quest_client: Any = None):
    """Resolve ``cfg.rep_sync_resolver``, composing one from ``cfg.personas`` when the consumer
    left the callable itself unset — the ``RunnerConfig.personas`` field's build path.

    Tri-state-flavored, but simpler than the deep-runner/context-assembler sentinels because there
    is no "auto-build a default" case here (an unset persona policy is a perfectly normal lane that
    runs every task as the plain assistant):
      * ``rep_sync_resolver`` already set (any consumer that wired one by hand, before ``personas``
        existed, or one that wants to fully replace the declarative policy) → returned UNCHANGED.
        An explicit callable always wins.
      * ``rep_sync_resolver`` unset AND ``personas`` set → ``runner.personas.build_persona_resolver``
        composes the equivalent callable from the declarative config.
      * neither set → ``None`` (unchanged): no persona routing, exactly today's behavior.

    Like ``resolve_deep_runner``, this does not mutate ``cfg`` itself — the caller (``Poller``)
    writes the result back, the same in-place style ``build_orchestrator`` uses for ``deep_runner``.
    ``runner.personas`` is imported LAZILY, only when ``cfg.personas`` is actually set, so building
    a RunnerConfig with no persona policy never pays for (or requires) that import — see the
    ``TYPE_CHECKING`` import note near the top of this module for why it can't be a top-level import.
    """
    if cfg.rep_sync_resolver is not None or getattr(cfg, "personas", None) is None:
        return cfg.rep_sync_resolver
    from .runner.personas import build_persona_resolver

    return build_persona_resolver(
        cfg.personas,
        provider=cfg.model_provider,
        quest_client=quest_client,
        team_id=cfg.team_id or "",
    )


@implementation("run-deep-work", "fast_edit")
def resolve_fast_edit_runner(cfg: RunnerConfig):
    """Build the in-process fast editor, or return None. OFF unless a ``file_writer`` is wired.

    This is the whole opt-in gate for quest-ai-runner's write capability, and it is deliberately a
    single condition with no env-var escape hatch and no "auto" tri-state: a consumer either handed
    the library a ``FileWriter`` or it did not. Nothing here can talk itself into write access.

    Returns None (silently — an absent writer is the normal case, not a misconfiguration) when no
    writer is wired, and also when there is no model provider to make the one call with. Never
    raises: a failure to build the optional cheap rung must never cost a consumer its runner.
    """
    writer = getattr(cfg, "file_writer", None)
    if writer is None or cfg.model_provider is None:
        return None
    try:
        from hubofhubs.act.fast_edit import FastEditRunner

        # ``cfg.model_provider`` is the MultiProvider-wrapped provider by the time this is called
        # from build_orchestrator (the wrap happens in place, earlier in that function), so the
        # model id this runner resolves routes to whichever backend owns it.
        return FastEditRunner(
            provider=cfg.model_provider,
            writer=writer,
            registry=ModelRegistry(cfg.model_provider, fallback=cfg.model_fallback or None),
        )
    except Exception:  # noqa: BLE001
        _log.warning("fast edit: could not build the FastEditRunner; deep work will use the "
                     "full deep runner only", exc_info=True)
        return None


@implementation("run-deep-work", "mcp_write")
def resolve_mcp_write_runners(cfg: RunnerConfig) -> List[Any]:
    """Build one ``MCPOperationRunner`` per configured ``MCPServerSpec`` that has non-empty
    ``writable_tools``, or ``[]``. OFF unless a spec's ``writable_tools`` is wired.

    Same opt-in shape as ``resolve_fast_edit_runner``: a single condition, no env-var escape
    hatch, no "auto" tri-state. ``RunnerConfig.mcp_servers`` already carries connection specs for
    the read-only ``MCPRetrievalAdapter`` side (Phase 1); a spec's ``writable_tools`` is a
    SEPARATE, additional allowlist a consumer sets explicitly to also grant writes through that
    same server -- being read-allowlisted (``allowed_tools``) grants nothing here. Each qualifying
    spec becomes its own namespaced ``MCPWriteAdapter`` wrapped in its own ``MCPOperationRunner``,
    the same "one spec, its own adapter" pattern the read side uses.

    Returns ``[]`` (silently -- no writable spec is the normal case) when ``mcp_servers`` is empty,
    no spec has ``writable_tools``, or there is no model provider to make the one call with. Never
    raises: a failure to build an optional cheap rung must never cost a consumer its runner.
    """
    if not cfg.mcp_servers or cfg.model_provider is None:
        return []
    runners: List[Any] = []
    try:
        from .adapters.mcp_write_adapter import MCPWriteAdapter
        from hubofhubs.act.mcp_operations import MCPOperationRunner

        registry = ModelRegistry(cfg.model_provider, fallback=cfg.model_fallback or None)
        for spec in cfg.mcp_servers:
            if not getattr(spec, "writable_tools", None):
                continue
            writer = MCPWriteAdapter(alias=spec.alias, spec=spec, writable_tools=spec.writable_tools)
            runners.append(MCPOperationRunner(
                provider=cfg.model_provider, writer=writer, registry=registry,
            ))
        if runners:
            _log.info("MCP write runners wired for: %s",
                      [s.alias for s in cfg.mcp_servers if getattr(s, "writable_tools", None)])
    except Exception:  # noqa: BLE001
        _log.warning("mcp write: could not build MCPOperationRunner(s); deep work will use the "
                     "remaining ladder only", exc_info=True)
    return runners


def resolve_deep_runner_ladder(cfg: RunnerConfig, *, warn: bool = True) -> List[Any]:
    """The ORDERED runners one deep goal may be tried with, cheapest rung first.

    Today this is at most three groups of rungs:

      1. ``FastEditRunner`` — present ONLY when the consumer wired a ``file_writer``. One model
         call, applied in process, against files already in the turn's context.
      2. one ``MCPOperationRunner`` per ``MCPServerSpec`` with non-empty ``writable_tools`` —
         present ONLY when a consumer configured a write allowlist for that server. One model
         call, executed in process through that server's own ``MCPWriteAdapter``.
      3. the resolved ``deep_runner`` — the full worker (``SubprocessGoalRunner`` by default).

    A consumer that changed nothing gets ``[SubprocessGoalRunner]``: a one-rung ladder, which the
    goal loop treats exactly as it treated a single runner before ladders existed. That equivalence
    is the point — this is additive, never a silent change to what already ships.

    All the cheap rungs may be absent (no writer, no writable MCP spec, and no worker because
    ``claude`` is not installed or execution was explicitly disabled), in which case this returns
    ``[]`` and the orchestrator reports honestly that nothing can execute.
    """
    ladder: List[Any] = []
    fast = resolve_fast_edit_runner(cfg)
    if fast is not None:
        ladder.append(fast)
    ladder.extend(resolve_mcp_write_runners(cfg))
    deep = resolve_deep_runner(cfg, warn=warn)
    if deep is not None:
        ladder.append(deep)
    return ladder


def resolve_recent_context_store(cfg: RunnerConfig):
    """Resolve the WARM recent-turn context store from config -- ON BY DEFAULT.

    Mirrors ``resolve_context_assembler``'s ``cards_dir`` resolution (same root, so recent-turn
    records live alongside the card store, under ``<cards_dir>/recent``) but is wired
    INDEPENDENTLY of which ``ContextAssembler`` ended up chosen: even a consumer that passed its
    own explicit ``context_assembler`` (or disabled it with ``None``) still gets the recent-turn
    fallback, since it is keyed purely by ``conv_id``/``quest_id`` and reads/writes its own small
    per-conversation JSON files under ``.quest-context/recent``. Disabled entirely when
    ``cfg.orchestrator.recent_context_enabled`` is False (env ``QAR_RECENT_CONTEXT``, read in
    ``cli.py``'s ``_config_from_env``). Construction never raises -- a bad path just yields
    ``None`` (the orchestrator behaves exactly as if no store were wired).
    """
    if not cfg.orchestrator.recent_context_enabled:
        return None
    try:
        from .core.recent_context import FileRecentContextStore
        root = cfg.corpus_root or os.getcwd()
        cards_dir = cfg.context_cards_dir or os.path.join(root, ".quest-context")
        return FileRecentContextStore(cards_dir)
    except Exception:  # noqa: BLE001 -- never let this wiring break runner construction
        return None


def resolve_anticipator(cfg: RunnerConfig, context_assembler: Any = None):
    """Resolve the ANTICIPATION engine from config -- OFF BY DEFAULT.

    Only when ``cfg.orchestrator.anticipation_enabled`` is True (env ``QAR_ANTICIPATION``, read in
    ``cli.py``'s ``_config_from_env``) does this build an ``Anticipator`` over a
    ``FilePredictionStore`` rooted at the same ``cards_dir`` the card/recent-context stores use
    (predictions live under ``<cards_dir>/predictions``). ``context_assembler`` is the ALREADY
    RESOLVED assembler build_orchestrator wired (passed in so the assembler is never constructed
    twice); it powers per-prediction context precompute and may be None (predictions then carry no
    precomputed bundle, matching still works). Construction never raises -- any failure yields
    ``None`` (the orchestrator behaves exactly as if no engine were wired).
    """
    if not cfg.orchestrator.anticipation_enabled:
        return None
    try:
        from .core.anticipation import Anticipator, FilePredictionStore
        root = cfg.corpus_root or os.getcwd()
        cards_dir = cfg.context_cards_dir or os.path.join(root, ".quest-context")
        refiner = _resolve_anticipation_refiner(cfg)
        return Anticipator(FilePredictionStore(cards_dir), assembler=context_assembler,
                           refiner=refiner)
    except Exception:  # noqa: BLE001 -- never let this wiring break runner construction
        return None


def _resolve_anticipation_refiner(cfg: RunnerConfig):
    """Build the OPTIONAL one-LLM-call anticipation refiner from config, or None.

    Only when ``cfg.orchestrator.anticipation_llm_enabled`` is True (env ``QAR_ANTICIPATION_LLM``,
    default OFF -- the lane stays zero-LLM by default) AND a ``model_provider`` is configured. The
    provider is ``cfg.model_provider`` (already wrapped with ``MultiProvider`` by
    ``build_orchestrator`` before this runs, per the repo's MultiProvider rule), and the model is
    resolved via ``ModelRegistry.resolve_tier("balanced")`` -- never a hardcoded model id. Returns
    the callable ``Anticipator`` expects: ``(candidates, recent_texts) -> (refinements, drops,
    followups)``. None (so the engine stays model-free) on any failure or when disabled."""
    if not getattr(cfg.orchestrator, "anticipation_llm_enabled", False):
        return None
    if cfg.model_provider is None:
        return None
    try:
        from .core.anticipation import (
            REFRESH_SYSTEM_PROMPT,
            build_refresh_prompt,
            parse_refresh_response,
        )
        from .core.model_registry import ModelRegistry
        provider = cfg.model_provider
        registry = ModelRegistry(provider, fallback=cfg.model_fallback or None)

        def refiner(candidates, recent_texts):
            model = registry.resolve_tier("balanced")
            prompt = build_refresh_prompt(candidates, recent_texts)
            raw = provider.answer(
                [{"role": "user", "content": prompt}], model=model,
                system=REFRESH_SYSTEM_PROMPT)
            return parse_refresh_response(raw, candidates)

        return refiner
    except Exception:  # noqa: BLE001 -- never let refiner wiring break runner construction
        return None


def resolve_tool_registry(cfg: RunnerConfig):
    """The tool catalog for this runner: ``cfg.tool_registry`` if the consumer wired one, else the
    standard tools + ``QAR_TOOLS_FILE`` custom tools from env. The Quest credentials the standard
    tools use fall back to the config's own ``quest_base_url``/``quest_api_key``/``team_id`` so a
    lane configured by file (not env) still gets them. Never raises: no tools on any failure."""
    if cfg.tool_registry is not None:
        return cfg.tool_registry
    try:
        from .core.tools import build_tool_registry
        env = dict(os.environ)
        if cfg.quest_base_url and not (env.get("QUEST_BASE_URL") or env.get("QUEST_API_URL")):
            env["QUEST_BASE_URL"] = cfg.quest_base_url
        if cfg.quest_api_key and not env.get("QUEST_API_KEY"):
            env["QUEST_API_KEY"] = cfg.quest_api_key
        if cfg.team_id and not env.get("QUEST_TEAM_ID"):
            env["QUEST_TEAM_ID"] = cfg.team_id
        return build_tool_registry(env)
    except Exception:  # noqa: BLE001 -- never let tool wiring break runner construction
        _log.warning("tool registry not built", exc_info=True)
        return None


def register_deepseek(providers: dict) -> None:
    """Add the DeepSeek provider to ``providers`` only when DEEPSEEK_API_KEY is set."""
    if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
        return
    try:
        providers["deepseek"] = make_provider("deepseek")
    except Exception as e:  # noqa: BLE001 -- an opt-in provider that cannot be built must not stop the others
        _log.debug(f"DeepSeek provider unavailable: {type(e).__name__}")
