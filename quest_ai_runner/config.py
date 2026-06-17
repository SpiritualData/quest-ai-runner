"""RunnerConfig — everything the CONSUMER supplies. NO consumer-specific values baked in.

A consumer (an integrating org, a chat backend, or a single-user lane) constructs a RunnerConfig
with its OWN Quest URL + key, the adapters it wants, its deep-runner, its model provider, and
(for orgs) the path to its skills/corpus. The library reads ALL specifics from here and
hardcodes none of them. Build the wired-up brain + poller via the factory helpers below.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger("quest-ai-runner.context")

from .core.adapters import (
    ContextAssembler,
    DeepRunner,
    EscalationSink,
    GuidanceProvider,
    ModelProvider,
    RetrievalAdapter,
)
# Bootstrap algorithm version + meta reader. file_context_store imports only from .core and
# ._walk (never from .config), so this top-level import carries no circular-import risk.
from .adapters.file_context_store import (
    _BOOTSTRAP_META_FILE,
    _BOOTSTRAP_VERSION,
    _read_bootstrap_meta,
)
from .core.model_registry import ModelRegistry
from .core.orchestrator import Orchestrator, OrchestratorConfig
from .resources import ResourceLimits

# Sentinel default for RunnerConfig.context_assembler: "build the default FileContextStore".
# Distinct from None (which means context handling is explicitly disabled) and from an instance
# (use that one). Lets context handling be ON BY DEFAULT while staying overridable and disableable.
_AUTO_CONTEXT = object()

# Per-attachment size cap for chat/file uploads and panel context-docs. Large by design so any
# reasonable document or image is accepted; anything over this is rejected by the multimodal
# handler (``core.attachments.prepare_attachments``) with a clear note rather than processed.
# 50 MB. Centralized here so both the runner config and the handler read the same number.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


@dataclass
class RunnerConfig:
    # --- Quest connection (per-consumer) ---
    quest_base_url: str = ""                 # e.g. https://api.example.org
    quest_api_key: str = ""                  # qsk_<hex>, the executor identity
    team_id: str = ""                        # team/org the poller serves (heartbeat + escalation)
    # Separate team_id for task discovery. When None, falls back to team_id. Set to "" to use
    # owner-scoped discovery (picks up null-team tasks) while still using team_id for heartbeat
    # and escalation — needed for personal/single-user lanes where tasks are owner-scoped.
    discovery_team_id: Optional[str] = None
    runner_label: Optional[str] = None       # human-readable tag sent on the env heartbeat (optional)
    env_id: Optional[str] = None             # which of the team's environments this runner is
                                             # (omit = the team's default env; set it when a team
                                             # attaches SEVERAL runners so each is its own env)

    # --- adapters (consumer chooses which) ---
    retrieval: Optional[RetrievalAdapter] = None     # FilesAdapter / CachedDbAdapter / a composite
    model_provider: Optional[ModelProvider] = None   # AnthropicProvider or another
    model_fallback: Optional[dict] = None            # override tier->model mapping (e.g. {"haiku": "gpt-4o", "sonnet": "claude-4"})
    deep_runner: Optional[DeepRunner] = None         # SubprocessGoalRunner or another worker
    escalation: Optional[EscalationSink] = None      # QuestDecisionSink (defaults from quest client)
    # The describer for image attachments the ANSWERING model can't view natively (a non-vision
    # model, or a text-only provider like the keyless CLI). The runner OWNS multimodal because the
    # text provider doesn't do it. When None and the answering provider is itself vision-capable,
    # the brain reuses it; supply a vision-capable provider here when the answering provider is not
    # (e.g. an AnthropicProvider alongside the keyless CLI) so chat images are transcribed.
    vision_provider: Optional[ModelProvider] = None
    # PRE-FLIGHT CONTEXT adapter (the fifth adapter role) — ON BY DEFAULT. Context handling is
    # core to running well, so a consumer that leaves this unset gets a default FileContextStore
    # wired automatically by ``build_orchestrator`` (cards under ``context_cards_dir`` or
    # ``<corpus_root|cwd>/.quest-context``). The orchestrator calls assemble() once before the loop
    # to inject reusable, file-pinned context and record() after the run to accumulate it.
    #   * leave UNSET (the _AUTO sentinel) → the default FileContextStore is built and used;
    #   * pass an INSTANCE → that assembler is used (a Quest-backed or composite one, etc.);
    #   * pass ``None`` explicitly → context handling is DISABLED (pure reactive-gather behaviour).
    context_assembler: Any = _AUTO_CONTEXT
    # Where the default FileContextStore writes its cards when context_assembler is left _AUTO.
    # None → ``<corpus_root or cwd>/.quest-context``. Cards are machine-written local state; the
    # consumer should gitignore this path (the runner repo gitignores ``.quest-context/``).
    context_cards_dir: Optional[str] = None
    # Optional VECTOR STORE for semantic orientation. When set (e.g. a QdrantVectorStore, local
    # by default or pointed at the backend's Qdrant), and context_assembler is left _AUTO, the
    # default becomes a HYBRID: keyword/IDF cards FUSED with vector search (the two are
    # complementary). The vector side runs agentic retrieval (LLM query-gen + parallel search +
    # LLM review) when model_provider is set. Leave None for keyword-only (zero-dependency).
    vector_store: Any = None
    # OPTIONAL USE-CASE-SPECIFIC INSTRUCTIONS provider (the GuidanceProvider role). When set, the
    # host app supplies a retrievable corpus of guidance cards (opaque text to the runner). The
    # orchestrator pre-selects the cards most relevant to each message into an "APPLICABLE
    # GUIDANCE" block before planning, and the planner can list_guidance / read_guidance on demand.
    # This lets a host app shrink its ALWAYS-ON core prompt to only what applies to every input.
    # Left None → no guidance, exactly today's behavior (purely additive).
    guidance_provider: Optional[GuidanceProvider] = None

    # --- the org's skills/corpus path (for orgs); generic, optional ---
    corpus_root: Optional[str] = None

    # --- tuning ---
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    poll_interval_seconds: float = 900.0
    poll_lookahead_minutes: float = 30.0
    max_concurrent_tasks: int = 2
    default_assignee_user_id: Optional[str] = None   # decision routing default

    # --- resource-aware throttling (opt-in; see quest_ai_runner/resources.py) ---
    # None = read the limits from the QAR_* env vars at poller construction (all unset = guard
    # disabled). Pass an explicit ResourceLimits to set them in code; ResourceLimits() disables
    # the guard regardless of env. When enabled, the poller stops PICKING UP new tasks while the
    # host is overloaded and resumes once resources recover — queued tasks just wait, unharmed.
    resource_limits: Optional[ResourceLimits] = None

    # --- AI-rep skill-file sync (opt-in; OFF by default) ---
    # When set, the poller pulls the latest AI-rep profile from Quest into the rep's local Claude
    # skill file RIGHT BEFORE running that rep's task, so the spawned agent behaves as the current
    # persona + learned corrections. This is consumer-specific (only the consumer knows how a task
    # maps to a (user_id, skill_dir)), so it's a resolver callable, not baked into the brain.
    # Given a task dict, return ``(user_id, skill_dir)`` to sync that rep, or ``None`` to skip.
    rep_sync_resolver: Optional[Callable[[Dict[str, Any]], Optional[Tuple[str, str]]]] = None
    # Sync DIRECTION for the opt-in rep flow above (only consulted when ``rep_sync_resolver`` is
    # set and resolves a target). Generic, with a sensible default:
    #   * "pull" (default) — Quest -> local skill file BEFORE the run, so the rep behaves as its
    #     current Quest self (persona + learned corrections) at execution time. No push-back.
    #   * "push" — local skill file -> Quest AFTER the run only (do NOT pull first). Use when the
    #     local file is the source of truth and Quest should be updated from it.
    #   * "both" — pull first (rep acts current), then push back after the run.
    # Pull (when in effect) ALSO feeds the rep's persona into the deep run automatically, so the
    # task runs AS that rep with no extra consumer glue. Push-back is best-effort: a sync failure
    # is logged and never fails the task. Validated by ``validate()`` (unknown value -> a problem).
    rep_sync_direction: str = "pull"

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
        if self.rep_sync_direction not in ("pull", "push", "both"):
            problems.append(
                "rep_sync_direction must be 'pull', 'push', or 'both' "
                f"(got {self.rep_sync_direction!r})")
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
    return ModelRegistry(cfg.model_provider, fallback=cfg.model_fallback or None)


def _cards_exist(cards_dir: str) -> bool:
    """True if the cards directory already holds at least one card file.

    Excludes the ``bootstrap_meta.json`` sidecar (algorithm metadata, not a card).
    """
    try:
        p = Path(cards_dir)
        return p.is_dir() and any(
            e.suffix == ".json" and not e.name.startswith(".")
            and e.name != _BOOTSTRAP_META_FILE
            for e in p.iterdir()
        )
    except OSError:
        return False


def _bootstrap_if_needed(
    keyword, *, root: str, cards_dir: str, provider=None,
    notify: Optional[Callable[[str], None]] = None,
) -> None:
    """Bootstrap or refresh the keyword store at startup. Always runs in the background.

    * **Cards exist**: launch a background thread calling ``refresh_stale()`` — re-indexes
      only changed/new files and skips everything unchanged.

    * **No cards** (first run): bootstrap the full tree in a background thread. The LLM
      identifies semantic topic cards; the user can chat immediately against whatever cards
      exist so far (even none). The vector arm seeds lazily on first assemble() call.

    ``notify`` (optional): a callable that receives a user-visible status string. Callers
    that have a console (e.g. the interactive CLI) pass ``console.dim`` so bootstrap events
    appear as system messages; background-only callers leave it ``None`` and events are
    logged instead.
    """

    def _tell(msg: str) -> None:
        """Emit msg to the notify callback when set, else to the log."""
        if notify is not None:
            notify(msg)
        else:
            _log.info(msg)

    if _cards_exist(cards_dir):
        # VERSION MISMATCH check: if the stored cards were built by an older bootstrap algorithm,
        # they are stale and we re-bootstrap in the background (the user can chat immediately
        # against whatever cards exist). Otherwise we just refresh stale cards.
        meta = _read_bootstrap_meta(cards_dir)
        stored_version = meta.get("version", 0)
        if stored_version < _BOOTSTRAP_VERSION:
            _tell(
                f"Context index: algorithm updated (v{stored_version} -> v{_BOOTSTRAP_VERSION}),"
                " re-indexing in background. Chat is ready now."
            )
            # Fall through to the background bootstrap path below (don't return early). The
            # incremental bootstrap dedups new/refreshed cards against the existing ones.
        else:
            # Version matches: just refresh stale cards in the background.
            _log.debug("context index: scanning %s for changes (background)", root)

            def _bg_refresh() -> None:
                try:
                    n = keyword.refresh_stale(root=root, provider=provider)
                    if n > 0:
                        _log.info("context index: refreshed %d card(s)", n)
                    else:
                        _log.debug("context index: all cards up to date")
                except Exception:  # noqa: BLE001
                    _log.debug("context index: refresh failed", exc_info=True)

            threading.Thread(target=_bg_refresh, daemon=True, name="qar-refresh").start()
            return

    else:
        # First run — no cards yet.
        _tell(
            "Context index: building for the first time in background."
            " Chat is ready now; context improves as indexing completes."
        )

    # A lock file prevents duplicate bootstraps when multiple sessions start simultaneously.
    lock_path = os.path.join(cards_dir, ".bootstrap.lock")

    def _bg() -> None:
        try:
            Path(cards_dir).mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "w")  # noqa: WPS515 — intentional file-descriptor lifetime
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _tell("Context index: another session is already building the index, skipping.")
                lock_fd.close()
                return
            try:
                n = keyword.bootstrap(root=root, provider=provider)
                _log.info("context index: ready — %d cards written to %s", n, cards_dir)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:  # noqa: BLE001
            _log.debug("context index: bootstrap failed", exc_info=True)

    threading.Thread(target=_bg, daemon=True, name="qar-bootstrap").start()


def resolve_context_assembler(
    cfg: RunnerConfig,
    *,
    notify: Optional[Callable[[str], None]] = None,
):
    """Resolve the context assembler from config — ON BY DEFAULT.

    Tri-state on ``cfg.context_assembler``:
      * ``_AUTO_CONTEXT`` (the field default, i.e. the consumer left it unset) → build the default
        ``FileContextStore`` so context handling works out of the box. Cards live under
        ``cfg.context_cards_dir`` or ``<corpus_root or cwd>/.quest-context``; ``repo_root`` is the
        same root so staleness can also read git blob shas. Construction never raises (a bad path
        just yields a store whose best-effort reads/writes no-op), so the runner still starts.
      * an INSTANCE → use it as-is (a Quest-backed or composite assembler, etc.).
      * ``None`` → context handling is explicitly DISABLED.
    """
    chosen = cfg.context_assembler
    if chosen is not _AUTO_CONTEXT:
        return chosen  # an explicit instance, or None to disable
    # Default-on: build a FileContextStore (keyword/IDF). Local import avoids a cycle.
    try:
        from .adapters import FileContextStore
        root = cfg.corpus_root or os.getcwd()
        cards_dir = cfg.context_cards_dir or os.path.join(root, ".quest-context")
        # auto_bootstrap=False: we manage the lifecycle ourselves via
        # _bootstrap_if_needed so lazy and explicit bootstrap don't race.
        keyword = FileContextStore(cards_dir, repo_root=root, auto_bootstrap=False)
        # If a vector store is configured, the default becomes a HYBRID: keyword/IDF FUSED with
        # semantic vector search (the two are complementary). Otherwise keyword-only.
        # Turn-history cards live alongside file cards: same root, subdir "turns".
        # This ensures turns are always at <corpus_root>/.quest-context/turns/ —
        # consistent regardless of cwd, and shared across CLI, chat, and the executor lane.
        from .core.turn_context_store import TurnContextStore
        from .core.composite_assembler import CompositeContextAssembler
        turns_dir = os.path.join(cards_dir, "turns")
        turn_store = TurnContextStore(turns_dir=turns_dir)

        # Resolve the vector store: explicit instance > auto-build local Qdrant > None.
        # When the [qdrant] extra is installed and no explicit store is configured, we auto-build
        # an embedded QdrantVectorStore (local filesystem, no server) so hybrid search is ON by
        # default without any consumer config.  Keyword-only is the fallback when the extra is
        # missing.
        vector_store = cfg.vector_store
        if vector_store is None:
            try:
                from .adapters import QdrantVectorStore as _QdrantVS
                if _QdrantVS is not None:
                    qdrant_path = os.path.join(cards_dir, "qdrant")
                    # QAR_EMBEDDER_BACKEND selects the embedding backend for the auto-built
                    # vector store: "voyage" uses Voyage AI (asymmetric document/query embedders,
                    # matching the backend's production setup), anything else (unset or
                    # "fastembed") uses the bare store's default local fastembed embedder.
                    backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
                    if backend == "voyage":
                        try:
                            from .adapters.qdrant_vector_store import make_voyage_embedder
                            vector_store = _QdrantVS(
                                path=qdrant_path,
                                embedder=make_voyage_embedder(input_type="document"),
                                query_embedder=make_voyage_embedder(input_type="query"),
                            )
                        except Exception:  # noqa: BLE001 — voyageai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=voyage but the voyageai "
                                "embedder could not be built — falling back to fastembed",
                                exc_info=True,
                            )
                            vector_store = _QdrantVS(path=qdrant_path)
                    elif backend == "openai":
                        try:
                            from .adapters.qdrant_vector_store import make_openai_embedder
                            embedder = make_openai_embedder()
                            vector_store = _QdrantVS(
                                path=qdrant_path,
                                embedder=embedder,
                                query_embedder=embedder,
                            )
                        except Exception:  # noqa: BLE001 — openai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=openai but the openai "
                                "embedder could not be built — falling back to fastembed",
                                exc_info=True,
                            )
                            vector_store = _QdrantVS(path=qdrant_path)
                    else:
                        vector_store = _QdrantVS(path=qdrant_path)
            except (ImportError, Exception):  # noqa: BLE001
                # qdrant-client / fastembed not installed, or construction failed: keyword-only.
                vector_store = None

        # Bootstrap (first run) or refresh stale cards (subsequent runs).
        # Always runs in the background — the vector arm seeds lazily on first
        # assemble() via seed_source, so blocking startup is never needed.
        _bootstrap_if_needed(keyword, root=root, cards_dir=cards_dir,
                             provider=cfg.model_provider, notify=notify)

        if vector_store is not None:
            from .adapters import HybridContextAssembler, VectorContextAssembler
            # Wire seed_source so the vector arm is seeded from the keyword store's
            # docstring cards on the first assemble() call (cold-start fix). Because
            # sync() is fingerprint-based, subsequent calls only re-embed changed
            # cards (AUTO-UPDATE). Never raises: errors in seed_source() are swallowed
            # by VectorContextAssembler._maybe_seed().
            vector = VectorContextAssembler(
                vector_store,
                provider=cfg.model_provider,
                seed_source=keyword.export_for_embedding,
            )
            file_assembler = HybridContextAssembler(keyword=keyword, vector=vector)
        else:
            file_assembler = keyword
        return CompositeContextAssembler([file_assembler, turn_store])
    except Exception:  # noqa: BLE001 — never let context wiring break runner construction
        return None


def build_orchestrator(
    cfg: RunnerConfig,
    *,
    status=None,
    notify: Optional[Callable[[str], None]] = None,
) -> Orchestrator:
    """Wire a domain-free Orchestrator from the consumer's adapters.

    Quest credentials and a retrieval adapter are NOT required here — they are
    needed only for the poller/runner lane. The brain works without a corpus
    (it simply won't do grounded read steps), making ``quest-ai-runner chat``
    usable without any Quest API key or corpus configured.

    ``notify`` (optional): forwarded to ``resolve_context_assembler`` and then to
    ``_bootstrap_if_needed``. Interactive callers (e.g. the CLI session) pass a
    console-print callback so bootstrap events appear as visible system messages
    rather than silent log entries.
    """
    _skip = {"quest", "retrieval adapter"}
    problems = [p for p in cfg.validate()
                if not any(kw in p for kw in _skip)]
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
        vision_provider=cfg.vision_provider,
        context_assembler=resolve_context_assembler(cfg, notify=notify),
        guidance=cfg.guidance_provider,
    )
