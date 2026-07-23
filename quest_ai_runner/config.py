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
import time
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
    _TFDFIDF_VERSION,
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
    model_providers: Optional[dict] = None           # multi-provider support: dict of name -> ModelProvider (e.g. {"anthropic": AnthropicProvider(), "gemini": GeminiProvider()})
    model_provider_overrides: Optional[dict] = None  # per-tier provider routing (e.g. {"best": "anthropic", "fast": "gemini"})
    deep_runner: Optional[DeepRunner] = None         # SubprocessGoalRunner or another worker
    # Named deep-runner registry. When non-empty and a ``deep_runner_classifier`` is
    # also provided, the orchestrator calls the classifier to SELECT which runner handles
    # each deep goal, rather than always using the single ``deep_runner``. The consumer
    # maps string keys (e.g. "code", "text", "delegate") to runner implementations.
    # Generic mechanism — consumer supplies both the keys and the classifier logic.
    deep_runners: Dict[str, Any] = field(default_factory=dict)
    # Callable that selects a runner key from ``deep_runners`` for a given goal.
    # Signature: (message: str, goal: str, brief: str) -> str
    # Must return a key present in ``deep_runners``; falls back to ``deep_runner`` on KeyError.
    # Left None -> ``deep_runner`` is always used (backward-compatible default).
    deep_runner_classifier: Optional[Any] = None
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
    # OPTIONAL REFERENCE RESOLVERS for source-agnostic context-card CONTENT. A context card holds
    # typed content items, each either a REFERENCE (resolved fresh to current content on use) or an
    # LLM note. The library ships built-in resolvers for ``file`` and ``note``; the data-backed
    # types (``collection``, ``conversation``, ``query``) are CONSUMER-INJECTED here so the library
    # stays generic (no consumer data access baked in). This is a ``{type: ReferenceResolver}`` dict
    # (a ReferenceResolver has ``resolve(locator, *, max_chars) -> str`` and NEVER raises). It is
    # threaded into the default FileContextStore when ``context_assembler`` is left _AUTO. Left None
    # → only the built-in file/note resolvers are wired; an un-wired reference type degrades to a
    # graceful unresolved-pointer line (never an error). Purely additive.
    reference_resolvers: Optional[dict] = None
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
    # OPTIONAL CONVERSATION STORE (storage-agnostic conversation-history retrieval). When set, the
    # orchestrator's User Input Understanding step (Step 1) can pull a relevant slice of the
    # CURRENT conversation (and, if needed, related past conversations) to rewrite a short/anaphoric
    # message ("ok do it", "the first one") into a self-contained goal condition before selecting
    # context. The reference impl ``adapters.SessionFileConversationStore`` reads local Claude
    # session files; a host can plug a Mongo-backed one behind the same Protocol. Left None → Step 1
    # is a no-op (self-contained inputs add ZERO latency), exactly today's behavior.
    conversation_store: Optional[Any] = None

    # --- the org's skills/corpus path (for orgs); generic, optional ---
    corpus_root: Optional[str] = None

    # --- tuning ---
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    poll_interval_seconds: float = 900.0
    poll_lookahead_minutes: float = 30.0
    max_concurrent_tasks: int = 2
    default_assignee_user_id: Optional[str] = None   # decision routing default

    # --- fast lane for INTERACTIVE work (context-requests from a live chat turn) ---
    # The background scan above (poll_interval_seconds, default 900s) is the right cadence for
    # scheduled/delegated work, but a live chat turn waiting on a remote-env context fetch needs an
    # answer in single-digit seconds. The poller runs a SECOND, always-on loop (its own daemon
    # thread) dedicated to interactive tasks only, so it never competes with or slows the normal
    # scan. Two strategies, chosen by ``wait_channel_enabled``:
    #   * long-poll (default) -- hold ONE bounded GET at a time (server blocks up to
    #     ``wait_timeout_seconds``), reconnecting immediately after each return. Near-instant
    #     delivery whenever the runner is up; env QAR_WAIT_CHANNEL ("0"/"false" disables).
    #   * short poll (fallback, or when the wait endpoint is unavailable) -- a plain interval poll
    #     over just the interactive queue, every ``context_poll_seconds``; env
    #     QAR_CONTEXT_POLL_SECONDS (0 disables the fast lane entirely, falling back to the normal
    #     background scan cadence for interactive work too).
    wait_channel_enabled: bool = True
    context_poll_seconds: float = 5.0
    wait_timeout_seconds: float = 25.0

    # --- resource-aware throttling (opt-in; see quest_ai_runner/resources.py) ---
    # None = read the limits from the QAR_* env vars at poller construction (all unset = guard
    # disabled). Pass an explicit ResourceLimits to set them in code; ResourceLimits() disables
    # the guard regardless of env. When enabled, the poller stops PICKING UP new tasks while the
    # host is overloaded and resumes once resources recover — queued tasks just wait, unharmed.
    resource_limits: Optional[ResourceLimits] = None

    # --- daily token budget (opt-in; see quest_ai_runner/usage.py) ---
    # Tracks shallow-orchestrator API tokens (plan + answer + context indexing) per UTC day and
    # pauses new task pickup when the daily limit is exceeded. Counts only the per-token-billed
    # providers (Anthropic, OpenAI, Gemini); the deep-runner runs on subscription and is excluded.
    # None = read QAR_DAILY_TOKEN_LIMIT from env at poller construction (unset = disabled). Pass
    # an explicit DailyUsageTracker to inject one in code (useful for tests and custom consumers).
    usage_tracker: Optional[Any] = None

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

    # --- quest <-> local folder sync (opt-in; OFF by default) ------------------
    # Maps a quest/goal id to a local folder that holds the real work for that quest (research,
    # drafts, code, a marketing plan, ...). ONE map, TWO consumers:
    #   * the poller (below) uses it to keep ``quest_folder_sync.QUEST_SYNC.md`` in that folder in
    #     sync with the quest's Quest state, right around running any task whose ``goal_id`` or
    #     ``quest_id`` is a key in this map;
    #   * the default FileContextStore (``resolve_context_assembler``) uses the SAME map to boost
    #     automated context selection toward that folder whenever a run's ``goal_id`` or
    #     ``quest_id`` matches (goal_id first, the same order the poller uses), so a user input
    #     tied to a quest grounds on its linked folder without extra consumer glue.
    # Left None (default) -> both behaviors are inert, exactly today's behavior.
    quest_folder_map: Optional[Dict[str, str]] = None
    # Sync DIRECTION for the opt-in quest-folder flow above (only consulted when a task's goal_id/
    # quest_id is a key in ``quest_folder_map``). Same three values as ``rep_sync_direction``:
    #   * "pull" (default) — Quest -> the folder's QUEST_SYNC.md BEFORE the run, so the folder
    #     reflects the quest's current state/notes at execution time. No push-back.
    #   * "push" — local sync file -> Quest AFTER the run only (post any queued local notes).
    #   * "both" — pull first, then push back after the run.
    # Best-effort in both directions: a sync failure is logged and never fails the task. Validated
    # by ``validate()`` (unknown value -> a problem).
    quest_folder_sync_direction: str = "pull"

    # --- Autopilot (opt-in; the recurring "autopilot pass" task, see runner/autopilot.py and
    # quest_autopilot_design.md). Both fields are inert unless a consumer creates a recurring
    # task with ``handler: "autopilot"`` (the executor routes those to ``AutopilotPass`` instead
    # of the normal deep-run path); nothing here changes behavior for any other task.
    # Team-wide daily cap on autopilot-created tasks (batches + goal proposals each count as one
    # unit). Default 3, per the design's starting number.
    autopilot_daily_budget: int = 3
    # The FALLBACK persona resolver (step 4 of the persona-routing chain, after a goal's own
    # ``assignee_rep_id`` and a day-matched/unrestricted quest ``autopilot.personas`` entry): a
    # consumer-supplied callable, given a goal dict, returning a rep_id or None. This is where a
    # consumer plugs in its OWN persona-inference logic (e.g. the personal lane's card-vote
    # resolver over character domain cards) — the library stays ignorant of it. Left None ->
    # autopilot tasks with no explicit persona run as the plain assistant.
    autopilot_persona_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None

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
        if self.quest_folder_sync_direction not in ("pull", "push", "both"):
            problems.append(
                "quest_folder_sync_direction must be 'pull', 'push', or 'both' "
                f"(got {self.quest_folder_sync_direction!r})")
        return problems


def _retrieval_has_web_search(retrieval) -> bool:
    """True if a web-search adapter (Tavily or provider-native) is already in the stack."""
    if retrieval is None:
        return False
    try:
        from .adapters.web_search_adapter import WebSearchAdapter as _WSA
        from .adapters.provider_web_search_adapter import ProviderWebSearchAdapter as _PWSA
        from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA
    except Exception:  # noqa: BLE001
        return False
    if isinstance(retrieval, (_WSA, _PWSA)):
        return True
    if isinstance(retrieval, _CRA):
        return any(isinstance(a, (_WSA, _PWSA)) for a in getattr(retrieval, "adapters", []))
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

    # web: the deep-runner browses via Claude Code's WebSearch/WebFetch (reads off the actual
    # tool gating), OR a WebSearchAdapter is wired into the retrieval stack (shallow web search
    # via Tavily). Both are honest: we detect what the config actually provides.
    web = False
    if deep is not None:
        sub_cfg = getattr(deep, "cfg", None)
        web_enabled = getattr(sub_cfg, "web_enabled", None)
        if callable(web_enabled):
            try:
                web = bool(web_enabled())
            except Exception:  # noqa: BLE001 — capability detection must never break the runner
                web = False

    # If no deep-runner web capability, check the retrieval stack for a web-search adapter:
    # a Tavily WebSearchAdapter (needs its key) OR a provider-native ProviderWebSearchAdapter.
    if not web and retrieval is not None:
        try:
            from .adapters.web_search_adapter import WebSearchAdapter as _WSA
            from .adapters.provider_web_search_adapter import ProviderWebSearchAdapter as _PWSA
            from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter as _CRA

            def _is_web(a) -> bool:
                if isinstance(a, _WSA):
                    return bool(getattr(a, "_api_key", ""))
                if isinstance(a, _PWSA):
                    return True
                return False

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


# --- Background context-index threads: OWNED, not fire-and-forget ---------------------------------
# ``_bootstrap_if_needed`` starts the index build/refresh on a daemon thread so chat is usable
# immediately. Daemon alone is not ownership: the thread can (and did) outlive whatever started it,
# still walking a corpus and shelling out ``git hash-object`` per file long after nobody will read
# the result. Every thread started here is registered with the store it is indexing, so
# ``shutdown_background_index()`` can close the store (stopping it at its next checkpoint) and join
# the thread. Entries are dropped once the thread is finished, so this never grows without bound.
_INDEX_THREADS: "List[Tuple[threading.Thread, Any]]" = []
_INDEX_THREADS_LOCK = threading.Lock()


def _register_index_thread(thread: threading.Thread, store: Any) -> None:
    """Record a background index thread and the store it is writing, and forget finished ones."""
    with _INDEX_THREADS_LOCK:
        _INDEX_THREADS[:] = [(t, s) for (t, s) in _INDEX_THREADS if t.is_alive()]
        _INDEX_THREADS.append((thread, store))


def shutdown_background_index(timeout: float = 10.0) -> None:
    """Stop every background context-index thread this process started, and wait for them to exit.

    Calls ``close()`` on each store being indexed (bootstrap/refresh then stop at their next
    checkpoint and spawn no further ``git`` subprocess) and joins each thread, up to ``timeout``
    seconds in total. Cards already written are kept; indexing is incremental and resumes on the
    next start.

    Call this whenever the owner of an orchestrator goes away: a consumer rebuilding its wiring, a
    long-lived service shutting a tenant down, a CLI about to exit, a test finishing. Without it, an
    index pass belongs to nobody and its stray subprocesses land in whatever the process does next.
    Idempotent and never raises.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    with _INDEX_THREADS_LOCK:
        pending = list(_INDEX_THREADS)
        _INDEX_THREADS.clear()
    for _, store in pending:
        try:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 — shutdown is best-effort, never raises
            _log.debug("context index: closing store during shutdown failed", exc_info=True)
    for thread, _ in pending:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining = 0.1  # always give a closed thread a moment to notice and unwind
            thread.join(timeout=remaining)
            if thread.is_alive():
                _log.warning("context index: background thread %s did not stop within the timeout",
                             thread.name)
        except Exception:  # noqa: BLE001
            _log.debug("context index: joining background thread failed", exc_info=True)


def _bootstrap_if_needed(
    keyword, *, root: str, cards_dir: str, provider=None, model: Optional[str] = None,
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
        # Check the global version and per-feature versions independently.
        # Global version bump → full LLM re-index for uncovered/stale files.
        # Per-feature version behind → cheap targeted migration (no LLM) until all cards updated.
        # Each is detected and handled separately so an unrelated feature never forces extra work.
        meta = _read_bootstrap_meta(cards_dir)
        stored_version = meta.get("version", 0)
        stored_features = meta.get("feature_versions", {})

        needs_full = stored_version < _BOOTSTRAP_VERSION
        needs_tfdfidf = stored_features.get("tfdfidf", 0) < _TFDFIDF_VERSION

        if needs_full:
            _tell(
                f"Context index: algorithm updated (v{stored_version} -> v{_BOOTSTRAP_VERSION}),"
                " re-indexing in background. Chat is ready now."
            )
        elif needs_tfdfidf:
            _tell(
                "Context index: computing tfdfidf signatures in background. Chat is ready now."
            )

        if needs_full or needs_tfdfidf:
            # Fall through to the background bootstrap path below. The bootstrap detects which
            # cards need tfdfidf migration and processes only those; per-entry "tfdfidf_v" is the
            # checkpoint so an interrupted run resumes from where it left off next startup.
            pass
        else:
            # Everything up to date: just refresh cards whose source files changed.
            _log.debug("context index: scanning %s for changes (background)", root)

            def _bg_refresh() -> None:
                try:
                    n = keyword.refresh_stale(root=root, provider=provider, model=model)
                    if n > 0:
                        _log.info("context index: refreshed %d card(s)", n)
                        _tell(f"Context index updated: {n} card(s) refreshed.")
                    else:
                        _log.debug("context index: all cards up to date")
                except Exception:  # noqa: BLE001
                    _log.debug("context index: refresh failed", exc_info=True)

            _refresh_thread = threading.Thread(target=_bg_refresh, daemon=True, name="qar-refresh")
            _register_index_thread(_refresh_thread, keyword)
            _refresh_thread.start()
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
                n = keyword.bootstrap(root=root, provider=provider, model=model)
                _log.info("context index: ready — %d cards written to %s", n, cards_dir)
                if n > 0:
                    calls = getattr(provider, 'call_count', 0)
                    msg = f"Context index ready: {n} card(s) indexed"
                    if calls > 0:
                        msg += f" ({calls} LLM calls)"
                    msg += "."
                    _tell(msg)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:  # noqa: BLE001
            _log.debug("context index: bootstrap failed", exc_info=True)

    _bootstrap_thread = threading.Thread(target=_bg, daemon=True, name="qar-bootstrap")
    _register_index_thread(_bootstrap_thread, keyword)
    _bootstrap_thread.start()


# --- Env vars the CARD-PERSISTENCE backend reads (documented here so config is discoverable) -------
# QAR_CARDS_BACKEND      — "file" (DEFAULT) or "qdrant". "file" keeps today's per-card-JSON behavior
#                          byte-for-byte. "qdrant" persists cards as points in ONE Qdrant collection
#                          via the generic ``QdrantCardRepository`` (no cards_dir), with a query-only
#                          ``QdrantCardVectorStore`` as the vector arm (each card embedded once).
# QAR_CARDS_COLLECTION   — (qdrant) the Qdrant collection cards live in (default "quest_ai_cards").
# QAR_QDRANT_URL         — (qdrant) the Qdrant server url; falls back to QAR_VECTOR_QDRANT_URL. When
#                          unset, an EMBEDDED local Qdrant under ``<cards_dir>/cards-qdrant`` is used.
# QAR_QDRANT_API_KEY     — (qdrant) the Qdrant api key; falls back to QAR_VECTOR_QDRANT_API_KEY.
# QAR_EMBEDDER_BACKEND   — (qdrant) "voyage" | "openai" | (unset/fastembed); selects the card embedder
#                          (same selector the auto vector store uses). Voyage uses asymmetric
#                          document/query embedders matching a backend's production setup.
# VOYAGE_EMBEDDING_SIZE  — (qdrant, voyage) the embedding dimension (default 1024).


def _qar_embedders(backend: str) -> tuple:
    """Resolve (doc_embedder, query_embedder, vector_size) for ``QAR_EMBEDDER_BACKEND``.

    "voyage" → asymmetric Voyage document/query embedders (dim from VOYAGE_EMBEDDING_SIZE, default
    1024); "openai" → one symmetric OpenAI embedder for both roles (dim 1536); anything else (unset
    or "fastembed") → the local fastembed default (one symmetric callable, dim 384). Returns
    ``(None, None, 0)`` on any failure so the caller can degrade. Never raises.
    """
    try:
        if backend == "voyage":
            from .adapters.qdrant_vector_store import make_voyage_embedder
            try:
                vsize = int(os.getenv("VOYAGE_EMBEDDING_SIZE") or 1024)
            except (TypeError, ValueError):
                vsize = 1024
            return (
                make_voyage_embedder(input_type="document"),
                make_voyage_embedder(input_type="query"),
                vsize,
            )
        if backend == "openai":
            from .adapters.qdrant_vector_store import make_openai_embedder
            emb = make_openai_embedder()
            return (emb, emb, 1536)
        # Local fastembed default (symmetric).
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

        def _fastembed(texts):
            return [list(v) for v in _model.embed(texts)]

        return (_fastembed, _fastembed, 384)
    except Exception:  # noqa: BLE001
        return (None, None, 0)


def _build_qdrant_card_repo_from_env(cards_dir: str) -> tuple:
    """Build a ``(QdrantCardRepository, QdrantCardVectorStore)`` pair from env, or ``(None, None)``.

    Reads ``QAR_CARDS_COLLECTION`` / ``QAR_QDRANT_URL`` / ``QAR_QDRANT_API_KEY`` and the
    ``QAR_EMBEDDER_BACKEND`` embedder. When no url is set, an EMBEDDED local Qdrant under
    ``<cards_dir>/cards-qdrant`` is used. Returns ``(None, None)`` on any failure (caller degrades to
    the filesystem backend). Never raises. NOTE: this default wiring is UNSCOPED (single-tenant) —
    a multi-tenant consumer wires its own scoped ``QdrantCardRepository`` per tenant instead.
    """
    try:
        from .adapters import QdrantCardRepository, QdrantCardVectorStore
        if QdrantCardRepository is None:  # [qdrant] extra not installed
            return (None, None)
        collection = (os.getenv("QAR_CARDS_COLLECTION") or "").strip() or "quest_ai_cards"
        url = (os.getenv("QAR_QDRANT_URL") or os.getenv("QAR_VECTOR_QDRANT_URL") or "").strip() or None
        api_key = (
            os.getenv("QAR_QDRANT_API_KEY") or os.getenv("QAR_VECTOR_QDRANT_API_KEY") or ""
        ).strip() or None
        path = None if url else os.path.join(cards_dir, "cards-qdrant")
        backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
        doc_emb, qry_emb, vsize = _qar_embedders(backend)
        if doc_emb is None or qry_emb is None:
            _log.warning("context index: QAR_CARDS_BACKEND=qdrant but no embedder could be built; "
                         "falling back to the file backend")
            return (None, None)
        repo = QdrantCardRepository(
            collection=collection, embedder=doc_emb, vector_size=vsize,
            client=None, url=url, api_key=api_key, path=path,
        )
        vstore = QdrantCardVectorStore(
            collection=collection, query_embedder=qry_emb,
            client=None, url=url, api_key=api_key, path=path,
        )
        return (repo, vstore)
    except Exception:  # noqa: BLE001
        _log.warning("context index: QAR_CARDS_BACKEND=qdrant repo build failed; falling back to "
                     "the file backend", exc_info=True)
        return (None, None)


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
        # provider= enables LLM relevance filtering of IDF candidates so only
        # cards genuinely relevant to the user's task are injected.
        from .core.model_registry import ModelRegistry as _MR
        _registry = _MR(cfg.model_provider, fallback=cfg.model_fallback or None)

        # CARD-PERSISTENCE backend: "file" (DEFAULT), "qdrant", or "quest" (API-backed).
        # "quest": when QAR_QUEST_API_URL + QAR_QUEST_API_KEY + QAR_USER_ID are set, cards
        # are persisted to quest-backend's /api/cards endpoint instead of locally. Cards carry
        # an inline preview so Quest AI can use them immediately without a local round-trip.
        # "qdrant": cards persist as points in one Qdrant collection (no cards_dir dependency).
        # "file" (default): per-card JSON files under cards_dir (stdlib only, zero deps).
        _cards_backend = (os.getenv("QAR_CARDS_BACKEND") or "").strip().lower() or "file"
        _card_repository = None
        _cards_vector_store = None  # set only for the qdrant backend (query-only, one embedding/card)

        # Quest API backend: auto-detected when the three required env vars are present and no
        # explicit backend override is set (or the override is "quest").
        _quest_api_url = (os.getenv("QAR_QUEST_API_URL") or "").strip()
        _quest_api_key = (os.getenv("QAR_QUEST_API_KEY") or "").strip()
        _quest_user_id = (os.getenv("QAR_USER_ID") or "").strip()
        _quest_cards_active = (
            _cards_backend == "quest"
            or (
                _cards_backend == "file"
                and _quest_api_url
                and _quest_api_key
                and _quest_user_id
            )
        )
        if _quest_cards_active and _quest_api_url and _quest_api_key and _quest_user_id:
            try:
                from .adapters.quest_api_card_repository import QuestApiCardRepository
                _card_repository = QuestApiCardRepository(
                    base_url=_quest_api_url,
                    api_key=_quest_api_key,
                    user_id=_quest_user_id,
                )
                _log.info(
                    "context index: using quest-backend card API (%s, user %s...%s)",
                    _quest_api_url,
                    _quest_user_id[:4] if len(_quest_user_id) >= 4 else _quest_user_id,
                    _quest_user_id[-4:] if len(_quest_user_id) >= 4 else "",
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "context index: QuestApiCardRepository build failed; falling back to "
                    "local file backend", exc_info=True,
                )
                _card_repository = None
        elif _cards_backend == "qdrant":
            _card_repository, _cards_vector_store = _build_qdrant_card_repo_from_env(cards_dir)

        # Auto-discover reference resolvers from any RESOLVABLE retrieval adapter the consumer wired
        # (structural: the adapter advertises a non-None ``reference_type`` + ``resolve_reference`` on
        # the RetrievalAdapter interface). Its OWN ``resolve_reference`` IS the resolver for its type,
        # so e.g. a wired ``GoogleChatAdapter`` makes its learned ``chat_thread`` references resolve
        # FRESH with no extra config. Consumer-supplied ``cfg.reference_resolvers`` win a collision.
        from .adapters.reference_resolver import collect_reference_resolvers
        _store_resolvers = {
            **collect_reference_resolvers(cfg.retrieval),
            **(cfg.reference_resolvers or {}),
        }
        keyword = FileContextStore(
            cards_dir, repo_root=root, auto_bootstrap=False,
            provider=cfg.model_provider,
            model=_registry.resolve_tier("balanced"),
            # Source-agnostic card content: built-in file/note resolvers, plus the data-backed types
            # (collection/conversation/chat_thread/query) from resolvable retrieval adapters and any
            # consumer-injected ``cfg.reference_resolvers``.
            reference_resolvers=_store_resolvers,
            # qdrant backend: route ALL card persistence through the Qdrant repo (no cards_dir). None
            # (file backend) keeps the default FilesystemCardRepository(cards_dir).
            card_repository=_card_repository,
            # Same map the poller consults for quest<->folder QUEST_SYNC.md sync (see
            # RunnerConfig.quest_folder_map): boosts automated context selection toward a quest's
            # linked folder whenever a run's quest_id matches.
            quest_folder_map=cfg.quest_folder_map,
        )
        # If a vector store is configured, the default becomes a HYBRID: keyword/IDF FUSED with
        # semantic vector search (the two are complementary). Otherwise keyword-only.
        # Turn-history cards live alongside file cards: same root, subdir "turns".
        # This ensures turns are always at <corpus_root>/.quest-context/turns/ —
        # consistent regardless of cwd, and shared across CLI, chat, and the executor lane.
        from .core.turn_context_store import TurnContextStore
        from .core.composite_assembler import CompositeContextAssembler
        turns_dir = os.path.join(cards_dir, "turns")
        turn_store = TurnContextStore(
            turns_dir=turns_dir,
            provider=cfg.model_provider,
            model=_registry.resolve_tier("balanced"),
        )

        # Resolve the vector store: explicit instance > qdrant-cards query-only arm > auto-build
        # local Qdrant > None. For the qdrant CARD backend, the repo already embedded each card on
        # write, so its vector arm is QUERY-ONLY (the QdrantCardVectorStore over the SAME collection)
        # and must NOT be replaced by an auto-built embedding store. When the [qdrant] extra is
        # installed and no explicit store is configured (file backend), we auto-build an embedded
        # QdrantVectorStore (local filesystem, no server) so hybrid search is ON by default without
        # any consumer config. Keyword-only is the fallback when the extra is missing.
        #
        # QAR_VECTOR_BACKEND gates the AUTO-BUILT arm (it never overrides an explicit
        # cfg.vector_store or the qdrant card backend's query-only arm):
        #   "auto" (default/unset) — attempt Qdrant, fall back to keyword-only with a warning
        #                            when it cannot be opened (today's behavior).
        #   "none" / "off"        — skip the Qdrant attempt entirely: keyword-only, silent
        #                            (no construction attempt, no warning log).
        #   "qdrant"              — require Qdrant: log an ERROR when it cannot be opened
        #                            (still degrades to keyword-only so the runner starts).
        vector_backend = (os.getenv("QAR_VECTOR_BACKEND") or "").strip().lower() or "auto"
        if vector_backend not in ("auto", "none", "off", "qdrant"):
            _log.warning(
                "context index: unrecognized QAR_VECTOR_BACKEND=%r; treating as 'auto'",
                vector_backend,
            )
            vector_backend = "auto"
        vector_store = cfg.vector_store
        if vector_store is None and _cards_vector_store is not None:
            vector_store = _cards_vector_store
        if vector_store is None and vector_backend not in ("none", "off"):
            try:
                from .adapters import QdrantVectorStore as _QdrantVS
                if _QdrantVS is not None:
                    if notify:
                        notify("Loading context index...")
                    # QAR_QDRANT_URL / QDRANT_URL: connect to a running Qdrant server.
                    # Preferred over embedded path — supports concurrent QAR instances.
                    # Falls back to an embedded local path when neither is set.
                    qdrant_url = (
                        os.getenv("QAR_QDRANT_URL")
                        or os.getenv("QAR_VECTOR_QDRANT_URL")
                        or os.getenv("QDRANT_URL")
                        or ""
                    ).strip() or None
                    qdrant_path = os.path.join(cards_dir, "qdrant")
                    # QAR_EMBEDDER_BACKEND selects the embedding backend for the auto-built
                    # vector store: "voyage" uses Voyage AI (asymmetric document/query embedders,
                    # matching the backend's production setup), anything else (unset or
                    # "fastembed") uses the bare store's default local fastembed embedder.
                    backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
                    _LOCAL_QDRANT_URL = "http://localhost:6333"

                    def _open_qdrant(**kw):
                        """Open QdrantVectorStore via URL (preferred) or embedded path.

                        URL mode (QAR_QDRANT_URL / QDRANT_URL set): connects to a running
                        Qdrant server — supports concurrent QAR instances with no locking.

                        Embedded mode (no URL): opens the local path. On lock contention
                        (another QAR instance holds it), falls back to localhost:6333 and,
                        if that is also unavailable, emits a visible warning and returns None.
                        """
                        if qdrant_url:
                            try:
                                return _QdrantVS(url=qdrant_url, **kw)
                            except Exception as e:  # noqa: BLE001
                                _log.warning("context index: Qdrant server at %s failed: %s",
                                             qdrant_url, e)
                                return None
                        # Embedded path.
                        try:
                            return _QdrantVS(path=qdrant_path, **kw)
                        except RuntimeError as e:
                            if "already accessed" not in str(e):
                                _log.warning("context index: Qdrant open failed: %s", e)
                                return None
                        except Exception as e:  # noqa: BLE001
                            _log.warning("context index: Qdrant open failed: %s", e)
                            return None
                        # Embedded path locked — try the local server as a fallback.
                        try:
                            store = _QdrantVS(url=_LOCAL_QDRANT_URL, **kw)
                            _log.info("context index: embedded Qdrant locked; connected to "
                                      "local server at %s", _LOCAL_QDRANT_URL)
                            return store
                        except Exception:  # noqa: BLE001
                            pass
                        msg = (
                            "Vector search unavailable: Qdrant path is locked by another QAR "
                            "instance and no local server responded at %s. "
                            "Set QDRANT_URL or QAR_QDRANT_URL to a running Qdrant server "
                            "(e.g. docker run -p 6333:6333 qdrant/qdrant)." % _LOCAL_QDRANT_URL
                        )
                        _log.warning("context index: %s", msg)
                        if notify:
                            notify(msg)
                        return None

                    if backend == "voyage":
                        try:
                            from .adapters.qdrant_vector_store import make_voyage_embedder
                            vector_store = _open_qdrant(
                                embedder=make_voyage_embedder(input_type="document"),
                                query_embedder=make_voyage_embedder(input_type="query"),
                            )
                        except Exception:  # noqa: BLE001 — voyageai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=voyage but the voyageai "
                                "embedder could not be built — falling back to fastembed",
                            )
                        if vector_store is None:
                            vector_store = _open_qdrant()
                    elif backend == "openai":
                        try:
                            from .adapters.qdrant_vector_store import make_openai_embedder
                            embedder = make_openai_embedder()
                            vector_store = _open_qdrant(
                                embedder=embedder,
                                query_embedder=embedder,
                            )
                        except Exception:  # noqa: BLE001 — openai missing/misconfigured
                            _log.warning(
                                "context index: QAR_EMBEDDER_BACKEND=openai but the openai "
                                "embedder could not be built — falling back to fastembed",
                            )
                        if vector_store is None:
                            vector_store = _open_qdrant()
                    else:
                        vector_store = _open_qdrant()
            except (ImportError, Exception):  # noqa: BLE001
                # qdrant-client / fastembed not installed, or construction failed: keyword-only.
                vector_store = None
            if vector_store is None and vector_backend == "qdrant":
                _log.error(
                    "context index: QAR_VECTOR_BACKEND=qdrant but no Qdrant vector store "
                    "could be opened. Install the [qdrant] extra (qdrant-client + fastembed) "
                    "or point QAR_QDRANT_URL / QDRANT_URL at a running Qdrant server. "
                    "Falling back to keyword-only context search."
                )

        # Bootstrap (first run) or refresh stale cards (subsequent runs).
        # Always runs in the background — the vector arm seeds lazily on first
        # assemble() via seed_source, so blocking startup is never needed.
        # Resolve a balanced-tier model for the bootstrap LLM calls.
        from .core.model_registry import ModelRegistry
        registry = ModelRegistry(cfg.model_provider, fallback=cfg.model_fallback or None)
        bootstrap_model = registry.resolve_tier("balanced")
        _bootstrap_if_needed(keyword, root=root, cards_dir=cards_dir,
                             provider=cfg.model_provider, model=bootstrap_model, notify=notify)

        if vector_store is not None:
            from .adapters import HybridContextAssembler, VectorContextAssembler
            # Wire seed_source so the vector arm is seeded from the keyword store's
            # docstring cards on the first assemble() call (cold-start fix). Because
            # sync() is fingerprint-based, subsequent calls only re-embed changed
            # cards (AUTO-UPDATE). Never raises: errors in seed_source() are swallowed
            # by VectorContextAssembler._maybe_seed().
            # Resolve the query model tier to a full model ID (not a bare tier name like "haiku")
            query_model = registry.resolve_tier("fast")
            vector = VectorContextAssembler(
                vector_store,
                provider=cfg.model_provider,
                query_model=query_model,
                seed_source=keyword.export_for_embedding,
            )
            # Wire the consolidating LLM pass: one holistic filter over the merged card set that
            # drops/reranks cards across arms and prunes their content items (content stays verbatim).
            # Uses the balanced tier (filtering/judgment work). Falls back to the mechanical merge
            # when no provider is wired or anything fails.
            consolidate_model = registry.resolve_tier("balanced")
            file_assembler = HybridContextAssembler(
                keyword=keyword,
                vector=vector,
                model_provider=cfg.model_provider,
                model=consolidate_model,
            )
        else:
            file_assembler = keyword

        # Claude session assembler: injects relevant Claude Code session digests
        # as pre-flight context alongside turn cards, using the same TF-DF-IDF
        # + recency selection as TurnContextStore.
        sessions_dir = os.getenv("QAR_CLAUDE_SESSIONS_DIR") or None
        try:
            from .adapters import ClaudeConversationsAdapter
            claude_assembler = ClaudeConversationsAdapter(
                corpus_root=root,
                sessions_dir=sessions_dir,
                # Wire the keyword card store so a cross-session recall hit becomes a LEARNED
                # ``conversation`` reference on the turn's active card (usage-recency tracked),
                # instead of being recomputed from the whole history every turn. Inert unless a
                # ``thread_card_id`` is in the assemble meta.
                card_store=keyword,
            )
            # The Claude adapter is itself resolvable (reference_type="conversation"); it is built
            # AFTER the store (it needs the store as its card_store), so it can't be discovered by
            # collect_reference_resolvers above. Register its resolver now so a LEARNED
            # ``conversation`` reference resolves FRESH by conv_id instead of dangling. Kept only if a
            # consumer/discovered ``conversation`` resolver isn't already wired (override=False).
            if getattr(claude_assembler, "reference_type", None):
                keyword.register_reference_resolver(
                    claude_assembler.reference_type,
                    claude_assembler.resolve_reference,
                )
        except Exception:  # noqa: BLE001
            claude_assembler = None

        assemblers = [file_assembler, turn_store]
        if claude_assembler is not None:
            assemblers.append(claude_assembler)
        return CompositeContextAssembler(assemblers)
    except Exception:  # noqa: BLE001 — never let context wiring break runner construction
        return None


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

    When Quest credentials are configured, QuestRetrievalAdapter is automatically
    added to the retrieval stack via CompositeRetrievalAdapter, enabling Quest-aware
    context retrieval without explicit configuration.

    Guidance: When cfg.guidance_provider is None, automatically builds a
    UniversalGuidanceProvider (loads guidance from standard locations, supports
    dynamic guidance via pluggable loaders). Guidance is injected into the system
    prompt as core behavior instructions.

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
    except Exception as e:  # noqa: BLE001
        _log.debug(f"OpenAI provider unavailable: {type(e).__name__}")

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

    # Web search as a STANDARD, key-free capability. If a Tavily WebSearchAdapter is already in
    # the retrieval stack (WEB_SEARCH_API_KEY set), keep it. Otherwise, unless web search is
    # explicitly disabled (WEB_SEARCH_ENABLED=false), wire a ProviderWebSearchAdapter when the
    # model provider supports NATIVE web search (Anthropic web_search tool / Gemini Google Search
    # grounding) — reusing the LLM key, so no separate web-search key is needed. This is what lets
    # ordinary AI tasks ("find marathons near Portland", "suggest a product") ground on the live web
    # without spawning Claude Code or requiring an external environment.
    _web_disabled = (os.getenv("WEB_SEARCH_ENABLED") or "").strip().lower() in ("false", "0", "off", "no")
    if not _web_disabled and not _retrieval_has_web_search(cfg.retrieval):
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
                    _log.info("Native web search enabled (%s, model=%s)", type(provider).__name__, web_model)
        except Exception as e:  # noqa: BLE001 — web search is optional; never break the build
            _log.debug("native web search not wired: %s", e)

    # Auto-enable guidance provider if not configured
    guidance = cfg.guidance_provider
    if guidance is None:
        from quest_ai_runner.core.guidance_provider import UniversalGuidanceProvider
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
        )

    # A default in-process inbox for mid-run user messages, so any interface (chat, Quest frontend,
    # ...) can push new messages and have them folded into the running goal loop automatically. A
    # consumer that already supplied one keeps it; a distributed deployment can pass its own.
    from .core.inbox import InMemoryInbox
    input_inbox = getattr(cfg, "input_inbox", None) or InMemoryInbox()

    context_assembler = resolve_context_assembler(cfg, notify=notify)
    return Orchestrator(
        retrieval=get_retrieval_adapter(cfg),
        provider=cfg.model_provider,
        registry=build_registry(cfg),
        deep_runner=cfg.deep_runner,
        deep_runners=cfg.deep_runners,
        deep_runner_classifier=cfg.deep_runner_classifier,
        escalation=cfg.escalation,
        config=cfg.orchestrator,
        status=status,
        vision_provider=cfg.vision_provider,
        context_assembler=context_assembler,
        guidance=guidance,
        input_inbox=input_inbox,
        conversation_store=cfg.conversation_store,
        recent_context=resolve_recent_context_store(cfg),
        anticipator=resolve_anticipator(cfg, context_assembler),
    )
