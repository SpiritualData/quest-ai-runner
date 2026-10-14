"""RunnerConfig — everything the CONSUMER supplies. NO consumer-specific values baked in.

A consumer (an integrating org, a chat backend, or a single-user lane) constructs a RunnerConfig
with its OWN Quest URL + key, the adapters it wants, its deep-runner, its model provider, and
(for orgs) the path to its skills/corpus. The library reads ALL specifics from here and
hardcodes none of them. Build the wired-up brain + poller via the factory helpers below.
"""
from __future__ import annotations

from .core.recipes import build_recipe_store_from_env

import dataclasses
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union



from .core.adapters import (
    EscalationSink,
    FileWriter,
    GuidanceProvider,
    ModelProvider,
    RetrievalAdapter,
)
# Bootstrap algorithm version + meta reader. file_context_store imports only from .core and
# ._walk (never from .config), so this top-level import carries no circular-import risk.
# MCPServerSpec is a plain dataclass with no dependency on .core or .config, so this top-level
# import (needed for the RunnerConfig.mcp_servers field type) carries no circular-import risk,
# same discipline as the file_context_store import above. It does NOT require the optional [mcp]
# package -- mcp_client.py only imports that lazily, inside its own connection seam.
from .adapters.mcp_client import MCPServerSpec
from .core.orchestrator import Orchestrator, OrchestratorConfig
from .resources import ResourceLimits

from .config_support import (  # noqa: F401
    _AUTO_CONTEXT,
    _AUTO_DEEP_RUNNER,
    _log,
)
from .config_context_index import (
    _bootstrap_if_needed,
    _build_qdrant_card_repo_from_env,
    _register_index_thread,
    shutdown_background_index,
)
from .config_resolvers import (
    register_deepseek,
    resolve_anticipator,
    resolve_deep_runner,
    resolve_deep_runner_ladder,
    resolve_mcp_write_runners,
    resolve_recent_context_store,
    resolve_rep_sync_resolver,
    resolve_tool_registry,
)

from .config_errors import (  # noqa: F401
    ConfigFileError,
    MAX_ATTACHMENT_BYTES,
)
from .config_environment import (
    apply_config_environment,
    load_quest_client,
    parse_decision_assignees,
    resolve_config_objects,
    resolve_context_assembler_stack,
)
from .config_retrieval import (  # noqa: F401
    _retrieval_has_web_search,
    apply_file_defaults,
    build_registry,
    derive_capabilities,
    fold_into_retrieval,
    get_retrieval_adapter,
)


if TYPE_CHECKING:
    # Type-checking only: importing runner.personas for real at module level would be circular
    # (quest_ai_runner.runner's __init__ imports .poller, which imports RunnerConfig from THIS
    # module). Every runtime use below imports it lazily instead, matching how this file already
    # defers its other runner/adapter imports (e.g. QuestClient inside build_orchestrator).
    from .runner.personas import PersonaResolverConfig


# TOML-file-expressible ``RunnerConfig`` fields: plain data (str/int/float/bool/dict/list-of-str)
# that round-trips through TOML with no Python object involved. Everything else on ``RunnerConfig``
# is a live adapter/callable/sentinel (a ``ModelProvider``, a ``DeepRunner`` instance, the
# ``_AUTO_*`` sentinels, a resolver callable, ...) that cannot be expressed as TOML data at all —
# accepting one of those key names in a file would silently build a config whose field holds a
# plain string or dict where an object belongs, which then fails far from the file that caused it,
# deep inside whatever first calls that field. ``RunnerConfig.from_file`` rejects those BY NAME,
# loudly, instead. Keep this in sync when a new plain-data field is added to ``RunnerConfig``.
_FILE_SCALAR_FIELDS = {
    "quest_base_url", "quest_api_key", "team_id", "discovery_team_id", "team_ids", "claim_teams", "org_id",
    "runner_label", "env_id", "lane_user_id",
    "model_fallback", "model_provider_overrides",
    "context_cards_dir",
    "channel_allowed_senders", "channel_ack_after_seconds", "channel_progress_min_seconds",
    "channel_turn_timeout_seconds", "channel_state_path",
    "corpus_root", "context_preamble",
    "poll_interval_seconds", "dispatch_interval_seconds", "poll_lookahead_minutes", "max_concurrent_tasks",
    "default_assignee_user_id", "decision_assignees", "decision_default_deadline_hours",
    "wait_channel_enabled", "context_poll_seconds", "wait_timeout_seconds",
    "rep_sync_direction",
    "quest_folder_map", "quest_folder_sync_direction", "quest_folder_zones", "quest_goal_sync",
    "quest_goal_updates_per_goal",
    "autopilot_ensure_pass_task", "autopilot_pass_time", "autopilot_daily_budget",
    "autopilot_settings_refresh_seconds", "autopilot_backpressure", "autopilot_adopt_recurring",
    "context_updates", "context_updates_state_path", "context_updates_first_look_days",
    "context_updates_judge_relevance", "context_updates_relevance_tier",
    "context_updates_judge_admission", "context_updates_admission_tier",
    "context_updates_relay_log_path",
    "rep_context_prefs", "context_preamble_doctrine", "vector_store", "mcp_drive",
    "context_sources_map", "context_sources_file", "drive_comments_auth", "inbound_mail_auth",
    "notion", "google_chat",
    "feedback_ledger", "feedback_ledger_path",
    "quest_folder_map_file",
    "state_path", "lane_label", "env_files", "env_aliases", "env",
    "extra",
}


# Special-cased NESTED tables: the TOML value is itself a table and becomes a specific dataclass
# (``PersonaResolverConfig``) rather than being assigned to the field as-is.
_FILE_SPECIAL_FIELDS = {"personas"}


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
    # The SET of teams this ONE lane discovers work from (QUEST_TEAM_IDS, comma-separated).
    #
    # Empty (the default) is single-team mode, byte-identical to everything before this field
    # existed: discovery is scoped by ``discovery_team_id``/``team_id`` exactly as documented
    # above, and every call the runner makes carries a single bare team value.
    #
    # Non-empty: this lane discovers from EVERY listed team in ONE request (the team query param
    # becomes a comma-joined list, which the backend's assistant-task listing accepts), rather
    # than needing one process per team. ``team_id`` keeps its existing, separate meaning -- the
    # lane's HOME team, used for the environment heartbeat, the escalation/decision default, and
    # the QuestClient instance default -- so a multi-team lane still has exactly one home.
    # Per-task work (persona resolution, rep context, the quest folder map) already resolves from
    # the task's own ``team_id``/``quest_id``, so it follows the task, not the home team.
    #
    # Precedence: ``discovery_team_id`` still WINS over this when it is set at all, INCLUDING an
    # explicit ``""`` (owner-scoped discovery). Setting both is a contradiction; the narrower,
    # older, more explicit knob is honoured so no existing deployment changes behaviour.
    team_ids: List[str] = field(default_factory=list)
    # CLAIM ALLOWLIST for an owner-scoped lane. Owner-scoped discovery (``discovery_team_id = ""``,
    # needed so a personal lane also sees teamless tasks) has NO team filter, so it lists every task
    # its account owns, on EVERY team, and races the team lanes for them. When non-empty, this lane
    # claims only tasks whose ``team_id`` is in this list, plus teamless tasks; a task on any other
    # team is left queued for the lane that serves that team. Empty (default) = no restriction.
    claim_teams: List[str] = field(default_factory=list)
    # Optional org-scoped registration for the environment heartbeat, loaded from QUEST_ORG_ID.
    # When set, the poller's heartbeat POSTs to /api/orgs/{org_id}/environment/heartbeat instead
    # of the team-scoped endpoint, registering this runner as available to EVERY team in that
    # org rather than just one team -- the mechanism a shared/org-wide runner deployment needs.
    # team_id is still required for task claiming/escalation regardless; org_id only changes
    # where the environment heartbeat/registration lands.
    org_id: str = ""
    runner_label: Optional[str] = None       # human-readable tag sent on the env heartbeat (optional)
    # The Quest user id this lane's API key authenticates AS (the app/service account behind
    # QUEST_API_KEY), from QAR_LANE_USER_ID. Unset = every behaviour below is exactly as before.
    #
    # It must be CONFIGURED rather than discovered: GET /api/auth/me answers 401 for an API key
    # (that route is for a signed-in human session), so the lane has no way to ask the backend who
    # it is. The operator who issued the key is the only party that knows.
    #
    # What it buys (2026-09-17): a task created against a quest is OWNED by that quest's owner,
    # and the task listing a lane discovers work from is owner-scoped, so a pass the lane creates
    # on a HUMAN-owned quest is invisible to the lane and never runs. Passing this id as the
    # created task's assignee_user_id makes the lane's own account the executor, which is what
    # discovery scopes on. The lane that ensures a pass exists is the lane that should run it, so
    # this is right in every case; on a quest the lane's account already owns it changes nothing.
    # It is also what lets the poller recognise a pass it can NEVER write to (one created before
    # this existed, owned by the human) instead of retrying an owner-scoped PATCH that 404s.
    lane_user_id: Optional[str] = None
    env_id: Optional[str] = None             # which of the team's environments this runner is
                                             # (omit = the team's default env; set it when a team
                                             # attaches SEVERAL runners so each is its own env)

    # --- adapters (consumer chooses which) ---
    retrieval: Optional[RetrievalAdapter] = None     # FilesAdapter / CachedDbAdapter / a composite
    # Generic MCP (Model Context Protocol) servers to fold into the retrieval stack -- READ-ONLY
    # foundation phase (see adapters/mcp_client.py + adapters/mcp_retrieval_adapter.py). Each spec
    # becomes its own namespaced MCPRetrievalAdapter, folded into cfg.retrieval via
    # CompositeRetrievalAdapter by build_orchestrator -- the same fold-in mechanism the native web
    # search adapter uses, not a parallel one. Needs the optional [mcp] extra only if this list is
    # non-empty; an empty list (the default) touches nothing MCP-related.
    mcp_servers: List[MCPServerSpec] = field(default_factory=list)
    # Fast, token-efficient LIVE WEB access (the ``WebResearch`` interface, ``core/adapters.py``):
    # snippet-first search plus a bounded single-page fetch, reached by the planner through its
    # own two read keys (``{"web": ...}``, ``{"web_page": ...}``) -- NEVER folded into
    # ``retrieval``/``CompositeRetrievalAdapter``, because that composite broadcasts every
    # grep/query to every member adapter, which would fire a paid, slow web search on an ordinary
    # corpus grep. Leave unset (the default) and ``build_orchestrator`` auto-builds one from env
    # via ``adapters.web_research.build_web_research_from_env`` (unless ``WEB_SEARCH_ENABLED=false``
    # or no backend is configured, both of which leave this ``None``). Set it explicitly to supply
    # a custom ``WebResearch`` (or a test fake); ``build_orchestrator`` never overwrites an
    # already-set value.
    web_research: Optional[Any] = None
    # A ``core.recipes.RecipeStore`` (operations replayed before context search). None builds one from
    # ``QAR_RECIPES=1`` (``QAR_RECIPES_DIR``, ``QAR_RECIPES_MIN_SCORE``), or leaves the feature off.
    recipe_store: Optional[Any] = None
    model_provider: Optional[ModelProvider] = None   # AnthropicProvider or another
    model_fallback: Optional[dict] = None            # override tier->model mapping (e.g. {"haiku": "gpt-4o", "sonnet": "claude-4"})
    model_providers: Optional[dict] = None           # multi-provider support: dict of name -> ModelProvider (e.g. {"anthropic": AnthropicProvider(), "gemini": GeminiProvider()})
    model_provider_overrides: Optional[dict] = None  # per-tier provider routing (e.g. {"best": "anthropic", "fast": "gemini"})
    # The deep worker — ON BY DEFAULT. ``core.goal_runner.SubprocessGoalRunner`` (Claude Code
    # headless via ``claude -p``) is the reference, and a consumer that leaves this unset gets one
    # wired automatically by ``build_orchestrator`` (see ``resolve_deep_runner``), pointed at
    # ``claude`` on PATH. ``adapters.AcpDeepRunner`` is an opt-in alternative that runs the same
    # contract over the Agent Client Protocol, which buys MID-TURN STEERING (a message queued while
    # the deep turn runs is injected into that turn instead of waiting for the next attempt); it
    # needs the [acp] extra and Node >= 22. Selecting one is just this field — see
    # docs/acp-deep-runner.md.
    #   * leave UNSET (the _AUTO sentinel) → the default SubprocessGoalRunner is built and used
    #     (or, if ``claude`` isn't on PATH, a loud warning and no runner);
    #   * pass an INSTANCE → that runner is used (AcpDeepRunner, a queue worker, a fake in tests);
    #   * pass ``None`` explicitly → deep execution is DISABLED, silently and deliberately.
    deep_runner: Any = _AUTO_DEEP_RUNNER             # SubprocessGoalRunner or another worker
    # WRITE ACCESS TO THE CONSUMER'S FILES — OFF BY DEFAULT, and the only switch that grants it.
    # Left None (the default), nothing in this library can modify a file: every reference
    # RetrievalAdapter is read-only, and a deep run can only change files by going through the
    # deep worker's own subprocess sandbox. Wiring a ``FileWriter`` here (the reference
    # implementation is ``adapters.files_writer.FilesWriter``) is the deliberate act of granting
    # write access, and it does exactly one thing: it puts an in-process ``FastEditRunner`` at the
    # front of the deep-runner ladder, so a bounded edit can be landed in one model call instead of
    # spawning a full agent, escalating to the normal deep worker whenever that is not enough.
    # See docs/fast-edit-runner.md for the containment/backup guarantees and the opt-in model.
    file_writer: Optional[FileWriter] = None
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
    # DIRECT TOOLS (core/tools.py): the catalog the brain can call in-process and deep runs can
    # call from their shell. None = build it from env in build_orchestrator (the standard tools
    # when Quest credentials exist, plus QAR_TOOLS_FILE custom tools; QAR_STANDARD_TOOLS=0 turns
    # the standard ones off). Pass a ToolRegistry to wire one in code (an empty one = no tools).
    tool_registry: Optional[Any] = None

    # --- LIVE CHANNEL lane (opt-in; see runner/channel_runner.py) ---------------------------
    # A live, two-way messaging channel (e.g. ``adapters.openclaw_channel.OpenClawChannel``) QAR
    # can hold a real-time conversation over -- a phone chat app, a Slack channel, etc. This is a
    # SEPARATE process/entry point (the ``channel`` CLI subcommand) from the task poller: a chat
    # message has no Quest task id to claim/PATCH, so it is never folded into ``poller.py``'s loop
    # or ``TaskExecutor``. None (default) = the lane does not run.
    channel_transport: Optional[Any] = None
    # Sender ids (the TRANSPORT's own ``InboundMessage.sender_id``, e.g. a channel user id) this
    # runner will act on. EMPTY (the default) means DENY ALL -- fail closed. This is a plain
    # membership test against operator config, never a decision based on anything a model wrote
    # (hard rule #3). An operator MUST explicitly list every sender allowed to reach the brain.
    channel_allowed_senders: List[str] = field(default_factory=list)
    # Seconds after a turn starts before ChannelSink sends one "still working on it" ack, if
    # nothing terminal has gone out yet. <= 0 disables the ack.
    channel_ack_after_seconds: float = 15.0
    # Minimum seconds between two milestone-progress sends within the SAME turn (throttling, not a
    # hard cap -- a turn with no milestones sends none at all).
    channel_progress_min_seconds: float = 20.0
    # Outer wall-clock safety net per turn, checked between ``run_stream`` events. <= 0 disables
    # it (the orchestrator's/deep-runner's own internal timeouts are still in effect either way --
    # this is an ADDITIONAL bound specific to the channel lane, not a replacement for them).
    channel_turn_timeout_seconds: float = 900.0
    # Dedup state file for the channel lane's ``StateStore`` (keyed on "<channel>:<message_id>").
    # None = in-memory only (no persistence across restarts) -- unlike the poller's state path,
    # which defaults to a file, this defaults to None because a consumer wiring a channel in tests
    # or a short-lived process should not have to think about a stray state file.
    channel_state_path: Optional[str] = None

    # --- the org's skills/corpus path (for orgs); generic, optional ---
    corpus_root: Optional[str] = None
    # Org/persona context prepended to every deep-run brief (the ``SubprocessConfig.context_preamble``
    # a consumer previously had to build a ``SubprocessGoalRunner`` by hand just to set). Read by
    # ``resolve_deep_runner`` when it builds the DEFAULT worker (``deep_runner`` left unset): the
    # auto-built ``SubprocessConfig`` gets ``context_preamble=cfg.context_preamble or ""``, so a
    # consumer that wants only "run as this team, with this doctrine" — no other worker
    # customization — never has to construct a ``SubprocessConfig`` at all. Ignored when
    # ``deep_runner`` is an explicit instance (that instance's own ``context_preamble`` wins, since
    # THAT is the consumer overriding this same thing on purpose). ``cli.py`` fills this from
    # ``QAR_CONTEXT_PREAMBLE_FILE`` (a file path — multi-line org doctrine is miserable as a bare env
    # var) or the shorter ``QAR_CONTEXT_PREAMBLE`` (inline, for a one-liner).
    context_preamble: Optional[str] = None

    # --- tuning ---
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    poll_interval_seconds: float = 900.0
    # How often due tasks are discovered and started. The long ``poll_interval_seconds`` scan then
    # only does housekeeping (heartbeat, folder/goal sync, autopilot pass producer), so a task
    # created now starts in seconds on the SAME dispatch path as scheduled work, real-time first.
    # <= 0 puts dispatch back on the long scan (the pre-split behaviour).
    dispatch_interval_seconds: float = 10.0
    poll_lookahead_minutes: float = 30.0
    max_concurrent_tasks: int = 2
    default_assignee_user_id: Optional[str] = None   # decision routing default
    # NAMED decision assignees: {role -> user id}, e.g. {"owner": "user_...", "operator":
    # "user_..."}. Lets a caller write ``create_decision(..., assignee="operator")`` instead of
    # threading a raw user id (and a lane's routing policy) through every call site. WHO approves
    # what is deployment policy, so it belongs in config; the library only resolves the name.
    # Env: ``QAR_DECISION_ASSIGNEES`` as a JSON object, or ``name=id`` pairs separated by commas.
    decision_assignees: Dict[str, str] = field(default_factory=dict)
    # Hours added to "now" as a decision's deadline when an escalation specified none. None (the
    # default) means no deadline is synthesized -- a decision only gets one when the escalation
    # asked for one explicitly. Env: ``QAR_DECISION_DEFAULT_DEADLINE_HOURS``. ``QuestDecisionSink``
    # also reads that env var directly, so setting it alone (no RunnerConfig wiring) still works.
    decision_default_deadline_hours: Optional[float] = None

    # --- fast lane for REAL-TIME work (context-requests from a live chat turn) ---
    # The background scan above (poll_interval_seconds, default 900s) is the right cadence for
    # scheduled/delegated work, but a live chat turn waiting on a remote-env context fetch needs an
    # answer in single-digit seconds. The poller runs a SECOND, always-on loop (its own daemon
    # thread) dedicated to real-time tasks only, so it never competes with or slows the normal
    # scan. Two strategies, chosen by ``wait_channel_enabled``:
    #   * long-poll (default) -- hold ONE bounded GET at a time (server blocks up to
    #     ``wait_timeout_seconds``), reconnecting immediately after each return. Near-instant
    #     delivery whenever the runner is up; env QAR_WAIT_CHANNEL ("0"/"false" disables).
    #   * short poll (fallback, or when the wait endpoint is unavailable) -- a plain interval poll
    #     over just the real-time queue, every ``context_poll_seconds``; env
    #     QAR_CONTEXT_POLL_SECONDS (0 disables the fast lane entirely, falling back to the normal
    #     background scan cadence for real-time work too).
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
    # DECLARATIVE alternative to writing ``rep_sync_resolver`` by hand (see ``runner/personas.py``
    # and ``docs/personas.md``). Two independent consumers each hand-wrote the same persona-
    # resolution machinery (a registry of ids -> skill folders, plus a policy for picking one from a
    # task) before this existed; set ``personas`` instead and ``resolve_rep_sync_resolver`` (called
    # by ``Poller.__init__``) composes the equivalent callable for you. An EXPLICIT
    # ``rep_sync_resolver`` always wins over this — set both only when you mean to fully replace the
    # declarative policy with your own callable. Left None (the default), nothing changes.
    personas: Optional["PersonaResolverConfig"] = None
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
    # Three-zone provenance convention inside a synced quest folder (runner/quest_folder_zones.py).
    # On (default), every pull scaffolds ``ai_driven/`` + ``human_context/``, writes the rule into
    # the folder's CLAUDE.md, and captures the person's own notes verbatim under
    # ``human_context/from_quest/``; runs on a foldered quest are also told the convention and told
    # to check the provenance ledger before building on AI-authored analysis.
    #
    # Why on by default: the failure it prevents is silent and compounding. An AI proposal that
    # nothing marks as a proposal is read as the brief by the next run, and by the run after that,
    # until the plan rests on a premise the person never agreed to. Set False for a consumer whose
    # folders are organised some other way.
    quest_folder_zones: bool = True
    # Keep a GOALS.md in each mapped folder, two-way (runner/quest_goal_sync.py). The quest state
    # block carries one OUTCOME and next_steps carries the next two or three things; neither is
    # the PLAN, so without this the only local answer to "what are all my goals" is whatever
    # hand-maintained file the folder grew. Direction follows quest_folder_sync_direction.
    quest_goal_sync: bool = True
    # How many of each goal's most recent updates a PULL renders under its bullet (newest first),
    # alongside the goal's description -- the per-goal check-in thread where a person writes what
    # they read/did/found on that one goal. Those notes otherwise never reach the local folder.
    # 0 turns the update lines off; descriptions still render either way. Push is unaffected: this
    # is pull-side rendering only (runner/quest_goal_sync.py).
    quest_goal_updates_per_goal: int = 3

    # --- Autopilot (opt-in per QUEST; see runner/autopilot.py and quest_autopilot_design.md).
    # Autopilot runs as a recurring "autopilot pass" task carrying ``task_kind: "autopilot"``,
    # which the executor routes to ``AutopilotPass`` instead of the normal deep-run path.
    #
    # Whether that pass task EXISTS is this runner's job (``Poller._ensure_autopilot_pass``).
    # It used to be nobody's: a user could switch a quest to Suggest/Act, the setting saved
    # correctly, and then nothing whatsoever happened, because no pass task had ever been created
    # to do the scanning. The feature was inert by construction, and silently so. Now any scan
    # that finds an opted-in quest with no open pass task creates one.
    # Set False for a deployment where something else owns that lifecycle.
    autopilot_ensure_pass_task: bool = True
    # The DEFAULT daily time (the runner's local clock) for a quest that names no ``run_time`` of
    # its own. Every opted-in quest gets its own recurring pass series either way -- this only
    # decides what hour the ones that never chose fire at, so there is no second kind of pass to
    # maintain. See ``Poller._quest_schedule_snapshot``.
    autopilot_pass_time: str = "07:00"
    # Team-wide daily cap on autopilot-created tasks (batches + goal proposals each count as one
    # unit). Default 3, per the design's starting number. MUST be at least the number of opted-in
    # quests that carry standing ``instructions``: each of those consumes one budget unit every
    # scheduled day (see runner/autopilot.py's always-work rule), and a quest that loses the
    # budget race on a given day produces NO output that day -- its own daily brief simply does
    # not arrive, with no error anywhere but a skip line on a pass row nobody mails.
    autopilot_daily_budget: int = 3
    # How often the poller re-reads every quest's autopilot settings (mode/run_time/run_timezone/
    # cadence/instructions) for the per-quest pass schedule (``Poller._quest_schedule_snapshot``).
    # Cached in between, so this trades read cost against convergence speed: a settings change (a
    # new run time, new instructions, mode flipped off) takes effect within one refresh interval,
    # not on the very next scan. Lower it for a deployment that wants faster convergence at the
    # cost of more reads; it is config rather than a constant for exactly that trade-off.
    autopilot_settings_refresh_seconds: int = 300
    # Whether a quest with an unfinished autopilot task is SKIPPED by the next pass. OFF by
    # default: an ideal colleague who raised a question or hit a blocker gets on with everything
    # that does not depend on the answer, rather than downing tools until someone replies. With it
    # off, a pass that finds unfinished work simply sees it in context and works around it. Turn it
    # on for a deployment that would rather have one batch at a time.
    autopilot_backpressure: bool = False
    # Default for adopting a quest's own due recurring tasks into the pass's batch (see
    # ``runner/autopilot.py``). A quest's OWN ``autopilot.adopt_recurring`` always wins; this is
    # only consulted when the quest does not state one, which includes deployments whose backend
    # predates that field. ``None`` means "no consumer opinion", i.e. off.
    autopilot_adopt_recurring: Optional[bool] = None
    # The FALLBACK persona resolver (step 4 of the persona-routing chain, after a goal's own
    # ``assignee_rep_id`` and a day-matched/unrestricted quest ``autopilot.personas`` entry): a
    # consumer-supplied callable, given a goal dict, returning a rep_id or None. This is where a
    # consumer plugs in its OWN persona-inference logic (e.g. the personal lane's card-vote
    # resolver over character domain cards) — the library stays ignorant of it. Left None ->
    # autopilot tasks with no explicit persona run as the plain assistant.
    autopilot_persona_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None

    # --- Automated context updates (see runner/context_updates.py) ---
    # ON. Every autopilot pass asks ONE engine what has changed on this quest since an assistant
    # last looked -- notes the person added, comments on a document the quest owns, files that
    # moved -- and carries the answer into the brief it composes, ref-tagged, with the run asked to
    # say in one line what it did with each. What a given quest watches is data the quest itself
    # carries (``autopilot.context_sources``), so nothing here names a source for anybody. Off,
    # a pass composes exactly what it composed before this existed.
    context_updates: bool = True
    # Where the per (quest, source) "an assistant last looked" stamps live. Left None, the poller
    # puts them beside its own state file, so the default deployment persists them without being
    # configured; a runner with no state file at all keeps them in memory for its lifetime, which
    # re-offers recent items after a restart rather than losing them.
    context_updates_state_path: Optional[str] = None
    # How far back a source looks for a quest nothing has ever read. On a first run everything
    # recent IS new, so this bounds what a newly opted-in quest is handed at once.
    context_updates_first_look_days: int = 14
    # Whether the engine puts USER-SCOPED captures to a model relevance judgment before a
    # run sees them (see runner/context_updates.llm_relevance_judge). On by default: without
    # it every capture reaches every card and the RUN does the filtering out loud, in the
    # output a person reads ("passed over: not this quest's domain"), which is the context
    # engine's work showing up as the assistant's chatter. Off, or with no model provider,
    # everything is delivered exactly as before.
    context_updates_judge_relevance: bool = True
    # Wrap whatever context assembler is configured with the per-rep one
    # (``adapters.rep_context_assembler.RepContextAssembler``): prepend the running rep's learned
    # ``context_prefs`` to the context view, and push one back when a run consults a real source.
    # Needs a persona policy, since it is the persona resolver that says which rep a run is for.
    rep_context_prefs: bool = False
    # Prepend ``core.context_doctrine.DEEP_CONTEXT_DOCTRINE`` to ``context_preamble``.
    # The library already composes the doctrine into the PER-REP preamble, but the static one a
    # rep-less run uses gets nothing, so a lane wanting its rep-less runs held to the same
    # discipline had to prepend it in Python. Default OFF: every existing lane's composed preamble
    # stays byte-identical.
    context_preamble_doctrine: bool = False
    # Declarative semantic arm: {url, collection_prefix, vector_size, embedding_model} fused with
    # the keyword/IDF store by a HybridContextAssembler. Assembling those four objects was the one
    # reason a lane kept a Python consumer for its context stack. Absent, or with its dependencies
    # or key missing, the keyword arm runs alone exactly as before.
    vector_store: Optional[Dict[str, Any]] = None
    # Declarative read-only Drive MCP server: {service_account_file, scopes, alias, allowed_tools}.
    # Folded into ``mcp_servers`` only when the key file is actually on disk, so a lane whose key
    # is not provisioned yet starts clean instead of failing.
    mcp_drive: Optional[Dict[str, Any]] = None
    # Model tier for that judgment. "balanced" per this repo's tier guidance for filtering.
    context_updates_relevance_tier: str = "balanced"
    # Whether the engine puts an ADMISSION-judged source's items (today: inbound_mail) through
    # "is this a real person writing to us, not bulk or automated mail" before a run or the
    # account's own asks record ever sees them (see runner/context_updates.llm_admission_judge).
    # A DIFFERENT question from context_updates_judge_relevance above -- see that function's own
    # docstring for why the two gates are kept separate rather than folded into one. On by
    # default: without it, whatever cleared the deterministic header check
    # (adapters.inbound_mail.is_bulk_mail) reaches a run, and the asks record, unfiltered.
    context_updates_judge_admission: bool = True
    # Model tier for the admission judgment. "fast", not "balanced": the header check already
    # removed what identifies itself as bulk mail for free, so this judge sees an already-narrowed
    # remainder and does not need the heavier tier a subject-matter judgment does.
    context_updates_admission_tier: str = "fast"
    # Where the "already relayed onto Quest" record lives (see
    # runner/context_updates.RelayedItems). Left None, it sits beside state_path, same default the
    # watermarks and the feedback ledger already use.
    context_updates_relay_log_path: Optional[str] = None
    # {quest_id: [spec, ...]} the DEPLOYMENT supplies, merged under whatever each card declares
    # for itself (the card always wins on a source both name). It exists because a card cannot
    # always carry its specs yet: a backend has to grow the field first, and until it does every
    # deployment is blocked on a schema change in another service. Live case: quest-backend's
    # autopilot settings reject an unknown key outright (422 extra_forbidden), so a quest could
    # not name a folder to watch at all. Same escape hatch ``quest_folder_map`` already is.
    # The record of what became of each thing a person asked for (``runner/feedback_ledger.py``).
    # On by default: without it, "handled" can only ever be inferred from timestamps, and a reply
    # that arrives after a request is not the same as the request being done.
    feedback_ledger: bool = True
    # Where that record lives. Defaults to beside ``state_path``, same as the watermarks.
    feedback_ledger_path: Optional[str] = None

    context_sources_map: Optional[Dict[str, Any]] = None
    # A JSON file holding that same map, for a deployment that grows it by hand over time (the
    # shape ``quest_folder_map_file`` has). Merged under ``context_sources_map``.
    context_sources_file: Optional[str] = None
    # Declarative form of ``drive_comments``: {service_account_file=..., subject=..., scopes=[...]}
    # A TOML file cannot hold a live client, and every deployment that wants one was otherwise
    # forced to keep a Python consumer alive for this single object. Resolved into
    # ``drive_comments`` by ``resolve_config_objects``.
    drive_comments_auth: Optional[Dict[str, Any]] = None
    # Declarative form of ``inbound_mail``: {service_account_file=..., subject=...,
    # allowed_addresses=[...], scopes=[...]}. Shaped exactly like ``drive_comments_auth`` and
    # resolved the same way (``resolve_config_objects`` -> ``_build_inbound_mail``), for the same
    # reason: minting a Workspace token is a deployment concern, not a card's. ``subject`` is the
    # identity this credential defaults to when ``allowed_addresses`` is left empty (it then
    # becomes the sole allowed mailbox); ``allowed_addresses`` is the actual, and only, code-side
    # limit on which mailbox a card's ``{"source": "inbound_mail", "mailbox": ...}`` spec may name
    # -- domain-wide delegation can otherwise impersonate ANY mailbox in the domain. See
    # ``adapters.inbound_mail`` for the read-only client this builds.
    inbound_mail_auth: Optional[Dict[str, Any]] = None
    # Declarative, opt-in, READ-ONLY Notion channel: {token_env = "NAME_OF_ENV_VAR" (or
    # token_file = "path"), database_ids = {alias = "<database id>"}, max_rows_per_database = 200}.
    # Resolved into ``notion_adapter`` by ``resolve_config_objects`` ONLY when a token is actually
    # available AND at least one database is named; otherwise it logs why and wires nothing. The
    # token itself is never in the file: only the NAME of the environment variable, or a path.
    notion: Optional[Dict[str, Any]] = None
    # Declarative, opt-in, READ-ONLY Google Chat channel: {service_account_file = "...", subject =
    # "...", scopes = [...] (default read-only), space_names = ["spaces/..."] (REQUIRED unless all_spaces = true),
    # all_spaces = false (opt-in: read EVERY space, group chat and DM the subject is in),
    # lookback_days = 30, assistant_senders = [...]}. FAILS CLOSED: a block with no ``space_names``
    # wires nothing and logs why, because a domain-wide-delegation credential can otherwise read
    # every space its subject is in. Resolved into ``google_chat_adapter``.
    google_chat: Optional[Dict[str, Any]] = None
    # A JSON file holding {quest_id: folder}. ``quest_folder_map`` takes the dict itself and there
    # was no pointer form, which is the one reason several lanes still read a JSON file in Python.
    quest_folder_map_file: Optional[str] = None

    # --- lane shape (so a lane is a config file, not a program) ---
    # Where the exactly-once signature dedup store lives (``QAR_STATE_PATH``'s file form).
    state_path: Optional[str] = None
    # Short name for this lane in shared log lines ("personal", "cantr").
    lane_label: str = ""
    # ``KEY=VALUE`` files loaded into the environment before anything else is resolved, in order.
    # Already-set process vars win, so a systemd unit's own Environment= still overrides. A lane
    # whose credentials live in one file, with a fallback to a shared one, expresses that here
    # instead of in code.
    env_files: Optional[List[str]] = None
    # {LIBRARY_VAR = "LANE_VAR"}: bridge a deployment's own env names onto this library's, for the
    # very common case of an .env that predates these conventions. TRUTHY-checked in both
    # directions, not presence-checked, because an .env with a blank placeholder line under the
    # library's own name would otherwise count as "already set" and the real value never applies.
    env_aliases: Optional[Dict[str, str]] = None
    # {VAR = "value"} applied with setdefault semantics, after files and aliases. The general
    # escape hatch: every ``QAR_*`` knob this library reads becomes file-expressible through it,
    # including the nested OrchestratorConfig ones a TOML field cannot reach.
    env: Optional[Dict[str, str]] = None
    # The Drive comments channel (``adapters.drive_comments.DriveComments``), supplied by the
    # consumer because only the consumer knows how its Google token is minted. Left None, the
    # ``drive_comments`` and ``drive_changes`` sources contribute nothing and every other source
    # runs exactly as before, so a quest that watches a folder simply sees no comments until a
    # deployment wires this.
    drive_comments: Any = None
    # The inbound-mail channel (``adapters.inbound_mail.InboundMail``), same reasoning as
    # ``drive_comments`` above. Left None, ``inbound_mail`` contributes nothing until a deployment
    # wires this (directly, or declaratively via ``inbound_mail_auth``).
    inbound_mail: Any = None
    # The Notion and Google Chat channels as live adapters (``adapters.notion_adapter.NotionAdapter``,
    # ``adapters.google_chat_adapter.GoogleChatAdapter``), or None. Supplied directly in Python, or
    # built from the ``notion`` / ``google_chat`` blocks above. ONE object serves both uses: it is
    # folded into the retrieval stack (so a run can read, grep and query it) and handed to the
    # context-update engine (so ``notion_database`` / ``google_chat`` card specs work). Left None,
    # a card naming either source gets an honest gap in its bundle, never an exception.
    notion_adapter: Any = None
    google_chat_adapter: Any = None

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

    @classmethod
    def from_file(cls, path: Union[str, "os.PathLike[str]"]) -> "RunnerConfig":
        """Build a ``RunnerConfig`` from a TOML file. Keys mirror ``RunnerConfig`` field names.

        Precedence with the environment: this is the FILE's baseline only. ``cli.py``'s
        ``_config_from_env`` (given ``config_path=`` or ``QAR_CONFIG_FILE``) layers environment
        variables on top of a file built this way, and an env var always wins on any field both
        set — see ``_layer_file_defaults`` there. ``from_file`` on its own reads no environment
        variable and applies no precedence; it only parses the file.

        Fails LOUDLY (raises :class:`ConfigFileError`) instead of silently dropping anything, on:
          * a missing or unreadable file, or invalid TOML;
          * a top-level key that is not a real ``RunnerConfig`` field name at all;
          * a top-level key that IS a real field name but holds a live adapter/callable/sentinel
            (a ``ModelProvider``, a ``DeepRunner`` instance, ``rep_sync_resolver``, ...) that cannot
            be expressed as TOML data — wire that one in Python, or via its matching ``QAR_*``
            environment variable, instead (see ``docs/writing-a-consumer.md``);
          * an invalid ``[personas]`` table (its keys must be ``PersonaResolverConfig`` fields).

        Uses the stdlib ``tomllib`` (Python 3.11+; this repo targets 3.12) — no new dependency.
        """
        # expanduser so a documented "~/..." path works from a script, a systemd unit and a shell
        # alike, instead of only from whichever of them happened to expand it first.
        p = Path(path).expanduser()
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigFileError(f"could not read config file {p}: {e}") from e
        try:
            import tomllib
        except ModuleNotFoundError as e:  # pragma: no cover - this repo's target is 3.12
            raise ConfigFileError(
                "RunnerConfig.from_file() needs the stdlib 'tomllib' module (Python 3.11+); this "
                "interpreter is older. Upgrade Python, or parse the file yourself and construct "
                "RunnerConfig(...) directly."
            ) from e
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigFileError(f"config file {p} is not valid TOML: {e}") from e
        if not isinstance(data, dict):
            raise ConfigFileError(f"config file {p} must be a table at the top level")

        data = dict(data)
        personas_table = data.pop("personas", None)

        all_field_names = {f.name for f in dataclasses.fields(cls)}
        not_file_expressible = all_field_names - _FILE_SCALAR_FIELDS - _FILE_SPECIAL_FIELDS
        unknown = set(data) - all_field_names
        unsupported = set(data) & not_file_expressible
        if unknown:
            raise ConfigFileError(
                f"config file {p} has key(s) that are not RunnerConfig fields at all: "
                f"{', '.join(sorted(unknown))}. Refusing to load rather than silently ignoring "
                f"them — a typo here would otherwise drop the setting instead of applying it."
            )
        if unsupported:
            raise ConfigFileError(
                f"config file {p} sets key(s) RunnerConfig cannot take from a file: "
                f"{', '.join(sorted(unsupported))} (each holds a live adapter, callable, or "
                f"object, not plain data). Wire it in Python, or via its matching QAR_* "
                f"environment variable, instead — see docs/writing-a-consumer.md."
            )

        kwargs: Dict[str, Any] = dict(data)
        if personas_table is not None:
            if not isinstance(personas_table, dict):
                raise ConfigFileError(f"config file {p}: [personas] must be a table")
            from .runner.personas import PersonaResolverConfig

            persona_fields = {f.name for f in dataclasses.fields(PersonaResolverConfig)}
            unknown_persona = set(personas_table) - persona_fields
            if unknown_persona:
                raise ConfigFileError(
                    f"config file {p}: [personas] has key(s) that are not PersonaResolverConfig "
                    f"fields: {', '.join(sorted(unknown_persona))}"
                )
            try:
                kwargs["personas"] = PersonaResolverConfig(**personas_table)
            except TypeError as e:
                raise ConfigFileError(f"config file {p}: [personas] table is invalid ({e})") from e

        try:
            return cls(**kwargs)
        except TypeError as e:
            raise ConfigFileError(f"config file {p} could not build a RunnerConfig: {e}") from e


def resolve_context_assembler(
    cfg: RunnerConfig,
    *,
    notify: Optional[Callable[[str], None]] = None,
):
    """Resolve the context assembler from config, then fuse on the declarative LAYERS.

    Two steps, because the layers WRAP a base rather than replace it: build (or accept) the base
    assembler, then apply ``vector_store`` and ``rep_context_prefs`` if the lane declared them.
    Assembling those layers by hand was the last reason a lane needed a Python consumer for its
    context stack. Each layer is skipped silently when unconfigured or unavailable.
    """
    return resolve_context_assembler_stack(
        _resolve_context_assembler_base(cfg, notify=notify), cfg)


def _resolve_context_assembler_base(
    cfg: RunnerConfig,
    *,
    notify: Optional[Callable[[str], None]] = None,
):
    """The base assembler — ON BY DEFAULT.

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

        # CARD-PERSISTENCE backend. The QUEST BACKEND is the DEFAULT whenever Quest credentials are in
        # the environment (QAR_QUEST_API_URL|QUEST_BASE_URL + QAR_QUEST_API_KEY|QUEST_API_KEY): cards
        # and their vectors live there, are embedded once server-side and searched server-side, and a
        # lane embeds and indexes no card locally. The acting user is whatever the key authenticates
        # (whoami). Cards a lane writes carry its team (QAR_TEAM_ID|QUEST_TEAM_ID).
        #   QAR_CARDS_BACKEND=local (alias "file"): the OPT-OUT. Per-card JSON files under cards_dir,
        #       with every local card_store_* behaviour (mounts, merge-into, move-root, read-only
        #       remote vectors) and the local auto-built vector arm exactly as before.
        #   QAR_CARDS_BACKEND=qdrant: one Qdrant collection (no cards_dir dependency).
        #   QAR_CARDS_BACKEND=quest: require the Quest backend. Without credentials this logs an ERROR,
        #       notifies, and runs LOCAL; the ERROR is the flag that it is not on the backend.
        _cards_backend = (os.getenv("QAR_CARDS_BACKEND") or "").strip().lower()
        if _cards_backend == "file":
            _cards_backend = "local"
        _card_repository = None
        _cards_vector_store = None  # query-only arm over the cards (qdrant backend, or the Quest backend)
        _quest_cards_active = False

        from .adapters.quest_api_card_repository import quest_card_settings_from_env
        _qs = quest_card_settings_from_env()
        _has_quest_creds = bool(_qs["base_url"] and _qs["api_key"])
        if _cards_backend == "quest" and not _has_quest_creds:
            _msg = ("context index: QAR_CARDS_BACKEND=quest but no Quest credentials are set "
                    "(QAR_QUEST_API_URL|QUEST_BASE_URL and QAR_QUEST_API_KEY|QUEST_API_KEY); "
                    "falling back to LOCAL cards, which are NOT shared with the Quest backend")
            _log.error(_msg)
            if notify:
                notify(_msg)
        elif _cards_backend in ("", "auto", "quest") and _has_quest_creds:
            try:
                from .adapters.quest_api_card_repository import (
                    QuestApiCardRepository, QuestApiCardVectorStore)
                _card_repository = QuestApiCardRepository(
                    base_url=_qs["base_url"],
                    api_key=_qs["api_key"],
                    user_id=_qs["user_hint"],
                    team_id=_qs["team_id"],
                    on_error=notify,
                )
                _user = _card_repository.resolve_user_id()
                _quest_cards_active = True
                _cards_vector_store = QuestApiCardVectorStore(_card_repository)
                _log.info(
                    "context index: using the Quest backend for cards and vectors (%s, user %s, team %s)",
                    _qs["base_url"], (_user[:4] + "..." if _user else "UNRESOLVED"),
                    _qs["team_id"] or "none (private cards)",
                )
            except Exception:  # noqa: BLE001
                _log.error(
                    "context index: QuestApiCardRepository build failed; falling back to the LOCAL "
                    "card backend (cards are NOT on the Quest backend)", exc_info=True,
                )
                _card_repository = None
                _cards_vector_store = None
                _quest_cards_active = False
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
            origin=keyword.origin_id(),
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
                            from .adapters.qdrant_vector_store import make_voyage_embedder, voyage_embedder_id
                            # Declare the REAL dimension up front. _open_qdrant used to omit
                            # vector_size here, so this always fell through to the constructor's
                            # 384 default and _adopt_embedding_dim logged a mismatch-and-adopt
                            # warning on every single process start once Voyage's actual 1024-dim
                            # output arrived -- correct behavior, but a warning that could never
                            # NOT fire is a config bug, not a real anomaly worth surfacing every
                            # run. Mirrors _qar_embedders' own VOYAGE_EMBEDDING_SIZE resolution.
                            try:
                                _voyage_vsize = int(os.getenv("VOYAGE_EMBEDDING_SIZE") or 1024)
                            except (TypeError, ValueError):
                                _voyage_vsize = 1024
                            vector_store = _open_qdrant(
                                vector_size=_voyage_vsize,
                                embedder=make_voyage_embedder(input_type="document"),
                                query_embedder=make_voyage_embedder(input_type="query"),
                                embedder_id=voyage_embedder_id(),
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
                            from .adapters.qdrant_vector_store import make_openai_embedder, openai_embedder_id
                            embedder = make_openai_embedder()
                            # Same fix as the voyage branch above: declare the real (fixed) OpenAI
                            # embedding dimension up front instead of relying on the 384 default
                            # and a same-process "adopting 1536" warning every run.
                            vector_store = _open_qdrant(
                                vector_size=1536,
                                embedder=embedder,
                                query_embedder=embedder,
                                embedder_id=openai_embedder_id(),
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
                             provider=cfg.model_provider, model=bootstrap_model, notify=notify,
                             card_repository=_card_repository)
        # Warm the card cache and scoring index while the session waits for its first message,
        # so the first turn's context budget is not spent parsing and weighting every card.
        if callable(getattr(keyword, "prewarm", None)):
            _prewarm_thread = threading.Thread(target=keyword.prewarm, daemon=True,
                                               name="qar-prewarm")
            _register_index_thread(_prewarm_thread, keyword)
            _prewarm_thread.start()

        if vector_store is not None:
            from .adapters import HybridContextAssembler, VectorContextAssembler
            # Wire seed_source so the vector arm is seeded from the keyword store's
            # docstring cards on the first assemble() call (cold-start fix). Because
            # sync() is fingerprint-based, subsequent calls only re-embed changed
            # cards (AUTO-UPDATE). Never raises: errors in seed_source() are swallowed
            # by VectorContextAssembler._maybe_seed().
            # Resolve the query model tier to a full model ID (not a bare tier name like "haiku")
            query_model = registry.resolve_tier("fast")
            # MOUNT MODE: the card arm may only see points of the stores this lane actually reads
            # (its own plus its readable mounts). The vector server answers anyone who can reach
            # it, so this payload filter is the fence; see adapters/card_store_mounts.py.
            if getattr(keyword, "mounts_enabled", False):
                from .adapters.card_store_mounts import StoreFencedVectorStore
                vector_store = StoreFencedVectorStore(
                    vector_store, keyword,
                    vector_space=(os.getenv("QAR_VECTOR_SPACE") or "").strip())
            vector = VectorContextAssembler(
                vector_store,
                provider=cfg.model_provider,
                query_model=query_model,
                # The hybrid below runs ONE holistic consolidating pass over the merged card set,
                # which judges these same vector hits as cards. Delegate the in-arm hit review to
                # it so hit relevance costs one model call per turn, not two (with no provider the
                # review was a no-op anyway, so this flag is safe unconditionally here).
                llm_review=False,
                seed_source=keyword.export_for_embedding,
                # Seeding walks every card (~12s export + fingerprint check on ~30k cards), which
                # used to run inside the first turn's vector arm and blow the assembly budget.
                seed_in_background=True,
            )
            # Start it now so the store is warm while the session waits for its first message.
            vector.start_seed()
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

    # Fast, token-efficient LIVE WEB access (``core.adapters.WebResearch``): built once here from
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
    # deployment without the newer module never regresses to no web capability at all. If a
    # Tavily WebSearchAdapter is already in the retrieval stack (WEB_SEARCH_API_KEY set), keep it.
    # Otherwise, unless web search is explicitly disabled (WEB_SEARCH_ENABLED=false), wire a
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
    # Resolve the deep worker's tri-state and WRITE IT BACK onto the config (the same in-place
    # style as the MultiProvider wrap above). Consumers and the chat UIs read ``cfg.deep_runner``
    # directly to decide whether execution is available, so the sentinel must never survive past
    # here — after build_orchestrator, that field is always a real runner or a real None.
    cfg.deep_runner = resolve_deep_runner(cfg)
    # The LADDER the goal loop indexes by attempt. Built from the already-resolved ``deep_runner``
    # plus, only if the consumer wired a ``file_writer``, the in-process fast editor in front of
    # it. ``cfg.deep_runner`` stays exactly what it was — a single runner — because consumers and
    # the chat UIs read that field to decide whether execution is available; the ladder is the
    # orchestrator's business, not a redefinition of the config field.
    deep_runner_ladder = resolve_deep_runner_ladder(cfg, warn=False)
    return Orchestrator(
        retrieval=get_retrieval_adapter(cfg),
        provider=cfg.model_provider,
        registry=build_registry(cfg),
        deep_runner=cfg.deep_runner,
        deep_runner_ladder=deep_runner_ladder,
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
        tools=resolve_tool_registry(cfg),
        web=cfg.web_research,
        recipes=cfg.recipe_store or build_recipe_store_from_env(),
    )
