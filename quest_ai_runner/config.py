"""RunnerConfig — everything the CONSUMER supplies. NO consumer-specific values baked in.

A consumer (an integrating org, a chat backend, or a single-user lane) constructs a RunnerConfig
with its OWN Quest URL + key, the adapters it wants, its deep-runner, its model provider, and
(for orgs) the path to its skills/corpus. The library reads ALL specifics from here and
hardcodes none of them. Build the wired-up brain + poller via the factory helpers below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core.adapters import DeepRunner, EscalationSink, ModelProvider, RetrievalAdapter
from .core.model_registry import ModelRegistry
from .core.orchestrator import Orchestrator, OrchestratorConfig


@dataclass
class RunnerConfig:
    # --- Quest connection (per-consumer) ---
    quest_base_url: str = ""                 # e.g. https://api.example.org
    quest_api_key: str = ""                  # qsk_<hex>, the executor identity
    team_id: str = ""                        # team/org the poller serves
    runner_label: Optional[str] = None       # human-readable tag sent on the env heartbeat (optional)
    env_id: Optional[str] = None             # which of the team's environments this runner is
                                             # (omit = the team's default env; set it when a team
                                             # attaches SEVERAL runners so each is its own env)

    # --- adapters (consumer chooses which) ---
    retrieval: Optional[RetrievalAdapter] = None     # FilesAdapter / CachedDbAdapter / a composite
    model_provider: Optional[ModelProvider] = None   # AnthropicProvider or another
    deep_runner: Optional[DeepRunner] = None         # SubprocessGoalRunner or another worker
    escalation: Optional[EscalationSink] = None      # QuestDecisionSink (defaults from quest client)

    # --- the org's skills/corpus path (for orgs); generic, optional ---
    corpus_root: Optional[str] = None

    # --- tuning ---
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    poll_interval_seconds: float = 900.0
    poll_lookahead_minutes: float = 30.0
    max_concurrent_tasks: int = 2
    default_assignee_user_id: Optional[str] = None   # decision routing default

    # --- AI-rep skill-file sync (opt-in; OFF by default) ---
    # When set, the poller pulls the latest AI-rep profile from Quest into the rep's local Claude
    # skill file RIGHT BEFORE running that rep's task, so the spawned agent behaves as the current
    # persona + learned corrections. This is consumer-specific (only the consumer knows how a task
    # maps to a (user_id, skill_dir)), so it's a resolver callable, not baked into the brain.
    # Given a task dict, return ``(user_id, skill_dir)`` to sync that rep, or ``None`` to skip.
    rep_sync_resolver: Optional[Callable[[Dict[str, Any]], Optional[Tuple[str, str]]]] = None

    extra: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """Return a list of human-readable config problems ([] = ok)."""
        problems: List[str] = []
        if not self.quest_base_url:
            problems.append("quest_base_url is required")
        if not self.quest_api_key:
            problems.append("quest_api_key (qsk_...) is required")
        if self.retrieval is None:
            problems.append("a retrieval adapter is required")
        if self.model_provider is None:
            problems.append("a model_provider is required")
        return problems


def derive_capabilities(cfg: RunnerConfig) -> Dict[str, bool]:
    """Derive the {web, corpus, code} capabilities the runner can HONESTLY report from its config.

    The backend's team-environment heartbeat carries what this runner can actually do, so the
    routing classifier can decide whether to route deferred work here. We read it straight off the
    wired adapters — never assert a capability we don't have:

      * ``corpus`` — a corpus/files retrieval adapter is configured (FilesAdapter, or any adapter
        bound to a ``corpus_root``). The runner can ground on the org's files/corpus.
      * ``code``   — a SubprocessGoalRunner (or any DeepRunner) is configured. The runner can do
        deep, code/goal-driven execution.
      * ``web``    — the deep-runner can BROWSE the live web. Our reference deep-runner spawns
        Claude Code, which ships WebSearch/WebFetch, so a configured SubprocessGoalRunner with the
        web tools allowed CAN web-research. We read this off the SubprocessConfig's actual tool
        gating (``web_enabled()``) — NOT a hardcode — so a consumer that pins tools without web
        honestly reports web:false, while the default (web-capable) state reports web:true.
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

    # code: a deep goal-runner (SubprocessGoalRunner is the reference) is wired.
    deep = cfg.deep_runner
    code = deep is not None

    # web: the deep-runner browses via Claude Code's WebSearch/WebFetch. Read the ACTUAL tool
    # gating off the SubprocessGoalRunner's config (web_enabled) rather than hardcoding. A non-
    # subprocess DeepRunner that doesn't expose web_enabled() is treated as non-web (conservative,
    # honest). No deep-runner at all → no way to browse → web:false.
    web = False
    if deep is not None:
        sub_cfg = getattr(deep, "cfg", None)
        web_enabled = getattr(sub_cfg, "web_enabled", None)
        if callable(web_enabled):
            try:
                web = bool(web_enabled())
            except Exception:  # noqa: BLE001 — capability detection must never break the runner
                web = False

    return {"web": web, "corpus": corpus, "code": code}


def build_registry(cfg: RunnerConfig) -> ModelRegistry:
    if cfg.model_provider is None:
        raise ValueError("model_provider is required to build a ModelRegistry")
    return ModelRegistry(cfg.model_provider)


def build_orchestrator(cfg: RunnerConfig, *, status=None) -> Orchestrator:
    """Wire a domain-free Orchestrator from the consumer's adapters."""
    problems = [p for p in cfg.validate() if "quest" not in p]  # the brain doesn't need Quest creds
    if problems:
        raise ValueError("RunnerConfig invalid for the brain: " + "; ".join(problems))
    return Orchestrator(
        retrieval=cfg.retrieval,
        provider=cfg.model_provider,
        registry=build_registry(cfg),
        deep_runner=cfg.deep_runner,
        escalation=cfg.escalation,
        config=cfg.orchestrator,
        status=status,
    )
