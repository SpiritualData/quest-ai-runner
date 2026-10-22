"""Console entry point for the poller (``quest-ai-runner``).

This is the thin CLI that runs the EXECUTOR lane. It builds a RunnerConfig from environment
variables (so a stranger's org can run it with no code) and starts the Poller in ``--once``
(cron) or loop (service) mode. NO consumer-specific defaults: every value comes from env.

Env it reads:
  QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID   — the Quest connection (key is qsk_...)
  QUEST_ORG_ID (optional)                        — when set, the environment heartbeat registers
                                                   at ORG scope (/api/orgs/{org_id}/environment/
                                                   heartbeat), making this runner available to
                                                   EVERY team in the org instead of just
                                                   QUEST_TEAM_ID. QUEST_TEAM_ID is still required
                                                   for task claiming/escalation regardless -- this
                                                   only changes where the heartbeat lands.
  QAR_CORPUS_ROOT                                — file root for the FilesAdapter (grounding);
                                                   also the default for QAR_DEEP_WORKING_DIR
  QAR_DEEP_WORKING_DIR (optional)               — working dir for the subprocess deep-runner;
                                                   defaults to QAR_CORPUS_ROOT, then the cwd
  QAR_CLAUDE_PATH (optional)                     — the worker binary (default: claude on PATH).
                                                   The deep runner is wired AUTOMATICALLY from
                                                   these two vars; nothing to configure in code.
  QAR_ANSWER_TIMEOUT (optional, seconds)         — per-call cap for the claude_cli planner/answer
                                                   backend (default 180; raise for large corpora)
  QAR_PLANNER_TIER (optional)                    — model tier for the planner step that picks
                                                   read/answer/deep (default balanced; use fast
                                                   to reduce cost, best for complex routing)
  QAR_PLANNER_MODEL (optional)                   — a model id for the routing decision ALONE
                                                   (the decide call); other calls that share
                                                   QAR_PLANNER_TIER keep that tier's model
  QAR_STATE_PATH (optional)                      — signature store path (default: ./qar_state.json)
  QAR_POLL_INTERVAL (optional, seconds)          — loop cadence (default 900)
  QAR_RUNNER_LABEL (optional)                    — human-readable tag sent on the env heartbeat
  QAR_ENV_ID (optional)                          — which of the team's environments this runner is
                                                   (omit = the team's default env; set a distinct id
                                                   per runner when a team attaches SEVERAL)
  QAR_LANE_USER_ID (optional)                    - the Quest user id QUEST_API_KEY authenticates
                                                   as (RunnerConfig.lane_user_id). Must be
                                                   configured, not discovered: /api/auth/me answers
                                                   401 for an API key. Set it so work this lane
                                                   creates on a quest somebody ELSE owns is
                                                   assigned to this lane's own account, since task
                                                   discovery is owner-scoped and would otherwise
                                                   never return it
  QAR_CONFIG_FILE (optional)                     — path to a TOML config file (RunnerConfig.from_file);
                                                   the `poll`/`chat`/`channel` subcommands' `--config`
                                                   flag does the same thing. File values are a
                                                   BASELINE only: any env var above that also sets a
                                                   field wins over the file. See
                                                   docs/writing-a-consumer.md.
  QAR_DISCOVERY_TEAM_ID (optional)               — separate team scope for task DISCOVERY only
                                                   (heartbeat/escalation keep using QUEST_TEAM_ID).
                                                   Unset = fall back to QUEST_TEAM_ID; set to "" for
                                                   OWNER-scoped discovery (a personal/single-user
                                                   lane whose tasks are created with no team_id).
  QUEST_CLAIM_TEAMS (optional)                   — comma-separated team ids an owner-scoped lane may
                                                   claim (plus teamless tasks); unset = no restriction
  QUEST_TEAM_IDS (optional)                      — comma-separated team ids this ONE lane
                                                   discovers work from (e.g. "team_a,team_b").
                                                   Unset/empty = single-team mode, unchanged.
                                                   QUEST_TEAM_ID stays the lane's HOME team
                                                   (heartbeat, escalation, client default) and is
                                                   still required. QAR_DISCOVERY_TEAM_ID, when set
                                                   at all (including ""), WINS over this.
  QAR_DECISION_ASSIGNEE (optional)               — user id that human-only confirm/decision
                                                   requests route to by default (RunnerConfig.
                                                   default_assignee_user_id / QuestDecisionSink's
                                                   fallback assignee)
  QAR_FILES_TOKEN                                - `serve-files` only: the bearer token every request
                                                   must carry (>= 16 characters; the server refuses to
                                                   start without it). Never passed on the command line.
  QAR_FILES_DENY (optional)                      - `serve-files` only: comma-separated root-relative paths
                                                   that are invisible to every operation (a personal
                                                   folder, a backups tree)
  QAR_FILES_ROOT / QAR_FILES_HOST / QAR_FILES_PORT (optional)
                                                 - `serve-files` only: the directory tree to serve
                                                   (also --root), the bind address (default 127.0.0.1)
                                                   and the port (default 8765). See docs/file-server.md
  QAR_DEEP_MAX_TURNS (optional)                  — hard per-attempt turn cap for the deep goal loop
                                                   (OrchestratorConfig.deep_max_turns; library
                                                   default 30 — raise it for a lane whose tasks
                                                   chain several real actions in one run)
  QAR_DEEP_AUTO_MODEL (optional)                 — "0"/"false"/"off" disables the automatic
                                                   starting model for unpinned deep runs (on by
                                                   default; see core/deep_model_selection.py)
  QAR_DEEP_MODEL_SIMPLE / _NORMAL / _HARD         — the Claude model a deep run STARTS on for work
                                                   the planner rated simple / normal / hard
                                                   (defaults haiku / sonnet / sonnet); the run
                                                   still escalates up QAR_DEEP_MODELS (or
                                                   haiku,sonnet,opus when unset) on a not-met goal
  ESOI_SELECT_QUEST_AI_RUNNER_RUN_DEEP_WORK (optional)
                                                 — the deep worker implementation: "subprocess"
                                                   (default, one-shot) or "stream_json" (a live
                                                   session; in `chat` it lets a typed message steer
                                                   the running worker and /pause and /resume work;
                                                   see docs/stream-json-runner.md)
  QAR_REP_SYNC_DIRECTION (optional)              — "pull" (default) | "push" | "both"; sets
                                                   RunnerConfig.rep_sync_direction. Only takes
                                                   effect once a rep_sync_resolver or a personas
                                                   policy (RunnerConfig.personas, see
                                                   runner/personas.py) is also wired.
  QAR_CONTEXT_PREAMBLE_FILE (optional)           — path to a file whose contents become
                                                   RunnerConfig.context_preamble (org/persona
                                                   doctrine prepended to every deep-run brief by the
                                                   AUTO-BUILT deep runner). Preferred over
                                                   QAR_CONTEXT_PREAMBLE (inline; still supported,
                                                   for a short one-liner) since multi-line prose is
                                                   miserable as a bare env var; the file wins when
                                                   both are set.
  QAR_TOOLS_FILE (optional)                      - ':'-separated TOML file(s) of custom direct
                                                   tools and standard-tool overrides the brain
                                                   calls with no deep run (docs/tools.md).
  QAR_STANDARD_TOOLS (optional)                  - "0" turns off the standard direct tools
                                                   (send_quest_email). Default on.
  QAR_AUTOPILOT_PASS_TIME (optional)             — default daily local time ("HH:MM") for an
                                                   opted-in quest that names no run_time of its own
                                                   (RunnerConfig.autopilot_pass_time; default "07:00")
  QAR_AUTOPILOT_ADOPT_RECURRING (optional)       — 1/true default for RunnerConfig.
                                                   autopilot_adopt_recurring (fold a quest's own due
                                                   recurring tasks into its autopilot pass); a
                                                   quest's own autopilot.adopt_recurring setting
                                                   always overrides this when it states one
  QAR_WAIT_CHANNEL (optional)                    — "0"/"false"/"off" disables the long-poll fast
                                                   lane for interactive context-requests (falls back
                                                   to a short poll, QAR_CONTEXT_POLL_SECONDS).
                                                   Default: enabled.
  QAR_CONTEXT_POLL_SECONDS (optional, seconds)    — fallback fast-lane poll interval, used only when
                                                   QAR_WAIT_CHANNEL is disabled (default 5; 0 turns
                                                   the fast lane off entirely, falling back to the
                                                   background scan's own poll_interval_seconds)
  QAR_WAIT_TIMEOUT_SECONDS (optional, seconds)    — how long each long-poll GET asks the backend to
                                                   hold the connection open (default 25, backend caps
                                                   it at 30)
  QAR_MAX_PARALLEL (optional)                    — max concurrent shallow LLM calls (context reads,
                                                   sub-questions, deep subtasks) within a single
                                                   gather/dispatch step (default 8). Each shallow call
                                                   is its own `claude` CLI subprocess when using the
                                                   keyless backend; lower this on a box that also runs
                                                   other Claude Code sessions to avoid CPU contention
                                                   pushing calls past their timeout.
  QAR_PLANNER_PROMPT_PROFILE (optional)         : "full" (default) or "compact". The compact
                                                   planner prompt is the same doctrine without the
                                                   answer-grounding gates a planner never uses, with
                                                   a condensed actions block and a schema carrying
                                                   its contract and not its prose. It cuts a routing
                                                   decision's input tokens by roughly 40 percent
                                                   with no measured accuracy cost, which is why a
                                                   deployment running routing on a cheap model wants
                                                   it. An unknown value degrades to "full". See
                                                   docs/cheap-model-routing.md.
  QAR_PLANNER_REACH_JUDGE (optional)            : "1"/"true"/"on" enables the reach judge (default:
                                                   off): one small question answered by a stronger
                                                   tier before planning, settling whether what the
                                                   request needs lives inside the readable sources,
                                                   outside them, or in the world right now. About 400
                                                   input tokens, cached per turn. It needs a consumer
                                                   to supply OrchestratorConfig.read_reach_summary,
                                                   since only a consumer knows what its own reads
                                                   reach; with no summary the judge stays inert.
  QAR_PLANNER_REACH_JUDGE_TIER (optional)       : the tier the reach judge runs on (default "balanced").
  QAR_READ_REACH_SUMMARY / QAR_READ_REACH_SUMMARY_FILE (optional)
                                                : the read_reach_summary the reach judge needs,
                                                   inline or from a file: what this lane's reads
                                                   reach and which environments take hand-offs.
                                                   Name EVERYTHING readable; anything left out
                                                   reads as "outside".
  QAR_CLI_PLAN_THINKING_TOKENS (optional)       : claude_cli backend only. Caps extended thinking on
                                                   every plan() call (routing decisions, the reach
                                                   judge, overseer/cascade reviews) via
                                                   MAX_THINKING_TOKENS; "0" turns it off. Measured on
                                                   routing only.
                                                   Answers and deep runs are never affected. Unset
                                                   leaves the CLI default.
  QAR_REUSE_NESTED_CARDS (optional)              — "0"/"false"/"no" disables it (default: on). When
                                                   on, ``FileContextStore.bootstrap()`` reuses any
                                                   sub-corpus that already has its own completed
                                                   bootstrap instead of re-running LLM topic discovery
                                                   over the same files — e.g. a corpus root at ``~/hq``
                                                   importing the cards a narrower QAR instance already
                                                   bootstrapped at ``~/hq/.../product``, at zero extra
                                                   LLM cost.
  --- card-index ceiling (how many cards a corpus may produce) ----------------------------------
  ONE rule, and it is structural rather than a quota: a topic card describes a FOLDER, so a folder
  gets a card. The size of the index is therefore a fact about the corpus (how many folders of
  real content it has) and not a number picked in advance. An earlier version carried a global
  budget of one card per 25 files and five cards per "area"; both were invented, and on a store
  that already held more cards than the budget allowed, the budget's only effect was to refuse to
  index anything new forever.
  QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER (optional)  — cards allowed per folder (default 1; 0 = no
                                                   limit). Raise it only for a corpus that
                                                   genuinely holds several topics in one folder.
  QAR_BOOTSTRAP_MAX_CARDS (optional)             — hard cap on cards in ONE bootstrap pass. Opt-in
                                                   with NO default, for an operator who wants to
                                                   bound a single pass; the folder rule is what
                                                   bounds the store.
  QAR_BOOTSTRAP_MAX_FILES (optional)             — stop walking after this many files. No default:
                                                   a truncated walk leaves the store INCOMPLETE.
  QAR_FOLDER_REVIEW (optional)                   — "0" turns off the folder relevance review
                                                   (default: on). Verdicts are cached in
                                                   ``<cards_dir>/folder_review.json``; a nested
                                                   repository or a duplicate tree is excluded for
                                                   good and nothing under it is re-judged. To
                                                   overturn one by hand, edit that folder's entry
                                                   and add ``"pinned": true`` so no later pass
                                                   overwrites your decision.
  QAR_SKIP_NESTED_REPOS (optional)               — "0" indexes directories carrying their own
                                                   .git/.hg/.svn (default: skip them).
  QAR_VECTOR_MAX_SEED_ITEMS (optional)           — cards ONE vector seed pass may embed
                                                   (default 1000; 0 = no cap). Applied before any
                                                   embedding call, so work over the cap costs
                                                   nothing.
  QAR_MAX_MEMORY_PERCENT (optional)              — pause new task pickup when system memory usage
                                                   exceeds this percent; resume when it recovers
  QAR_MIN_FREE_MEMORY_MB (optional)              — pause when available memory drops below this MB
  QAR_MAX_LOAD_PER_CORE (optional)               — pause when 1-min load average per CPU core
                                                   exceeds this (e.g. 2.0)
  QAR_RESOURCE_RESUME_MARGIN (optional, %)       — hysteresis: a tripped metric must clear its
                                                   limit by this percent before resuming (default 10)
  QAR_RESOURCE_CHECK_INTERVAL (optional, secs)   — re-check cadence while paused (default 30)
  QAR_DAILY_TOKEN_LIMIT (optional, tokens)       — daily cap on shallow API tokens (input + output
                                                   combined). Default: 2,000,000 tokens/day when
                                                   unset. When exceeded, the poller pauses new task
                                                   pickup until midnight UTC and any in-flight chat
                                                   turn returns a message with instructions to raise
                                                   the cap. Set to a larger number to raise it; set
                                                   to 0 or "off" to disable entirely. The deep-runner
                                                   (Claude Code, subscription-based) is NOT counted.
  QAR_DAILY_USAGE_PATH (optional)                — JSON file for persisting today's token count
                                                   across restarts (default: ./qar_daily_usage.json;
                                                   gitignored by this repo).
  QAR_MODEL_BACKEND (optional)                   — "anthropic" | "claude_cli". Default: auto —
                                                   "anthropic" if ANTHROPIC_API_KEY is set, else
                                                   "claude_cli" (keyless, via the subscription login).
  DEEPSEEK_API_KEY (optional)                    — registers the DeepSeek provider (OpenAI-compatible
                                                   API); model ids starting "deepseek" route to it.
                                                   Never auto-bucketed into a tier: pin it with
                                                   QAR_MODEL_<TIER>=<deepseek id> to use it
  DEEPSEEK_BASE_URL (optional)                   — default https://api.deepseek.com
  DEEPSEEK_THINKING (optional)                   — "1" lets DeepSeek think before answering (slower,
                                                   more output tokens); default off
  QAR_VECTOR_BACKEND (optional)                  — gates the auto-built context vector store:
                                                   "auto" (default) attempts Qdrant and falls
                                                   back to keyword-only search with a warning
                                                   when it cannot be opened; "none"/"off" skips
                                                   the Qdrant attempt entirely (keyword-only,
                                                   silent); "qdrant" requires Qdrant and logs
                                                   an error when it is unavailable (still
                                                   degrades to keyword-only). Never overrides
                                                   an explicitly configured vector store.
  QAR_CARD_MOUNT (optional)                      — "mount" reads the readable card stores nested in
                                                   or above the corpus IN PLACE and read-only (no copy,
                                                   no re-embedding, the reader indexes nothing under
                                                   them); "copy" (default) is the legacy copy-import.
                                                   See docs/context-card-stores.md.
  QAR_VECTOR_SPACE (optional)                    — names the vector server/collection this lane uses,
                                                   to match the owners' declared space (mount mode).
  QAR_VECTOR_UNATTRIBUTED (optional)             — "1" also serves pre-fence task associations that
                                                   carry no store id (single-tenant stores only).
  QAR_EMBEDDER_BACKEND (optional)                — embedding backend for the auto-built context
                                                   vector store: "voyage" (Voyage AI, needs voyageai
                                                   package + VOYAGE_API_KEY / VOYAGE_MODEL),
                                                   "openai" (OpenAI, needs openai package +
                                                   OPENAI_API_KEY; model via QAR_OPENAI_EMBEDDING_MODEL,
                                                   default text-embedding-3-small), or "fastembed"
                                                   / unset (default, local ONNX, no API key).
                                                   Falls back to fastembed if the chosen backend's
                                                   package is missing.
  ANTHROPIC_API_KEY (optional)                   — only for the "anthropic" backend (per-token
                                                   billing). NOT needed for the keyless claude_cli
                                                   backend, which runs on Claude Code's subscription.
  WEB_SEARCH_ENABLED (optional)                 — web search is ON by default and needs NO extra
                                                   key (falls back to the model provider's native
                                                   tool). Set to "false" to disable it entirely.
                                                   See docs/web-search.md.
  QAR_WEB_SEARCH_BACKEND (optional)             — which search backend to use: "auto" (default;
                                                   picks the first of serper/brave/tavily/searxng
                                                   whose key/URL is set, else falls back to the
                                                   model provider's native search), or an explicit
                                                   "serper" | "brave" | "tavily" | "searxng" |
                                                   "gemini" | "provider".
  SERPER_API_KEY (optional)                     — serper.dev key for the Serper backend.
  BRAVE_SEARCH_API_KEY (optional)               — Brave Search API key for the Brave backend.
  TAVILY_API_KEY (optional)                     — Tavily key (tvly_...) for the Tavily backend.
                                                   WEB_SEARCH_API_KEY is the legacy name for this
                                                   and still works.
  SEARXNG_URL (optional)                        — base URL of a self-hosted SearXNG instance for
                                                   the searxng backend.
  QAR_WEB_SEARCH_MODEL (optional)               — model id for the Gemini-grounding backend
                                                   (default gemini-2.5-flash-lite); needs a Gemini
                                                   key via GEMINI_API_KEY / GOOGLE_API_KEY /
                                                   GOOGLE_AI_API_KEY. This is also what gives a
                                                   claude_cli lane (no Anthropic web_search tool)
                                                   shallow web search.
  WEB_SEARCH_MAX_RESULTS (optional)             — max results per web search call (default 5).
  QAR_WEB_PAGE_TOKEN_BUDGET (optional)          — token budget for one FOCUSED page read: the
                                                   passages most relevant to ``focus`` (see
                                                   ``{"web_page": ...}`` reads; default 3000).
  QAR_WEB_PAGE_FULL_TOKEN_BUDGET (optional)     — token budget for one part of a WHOLE-document
                                                   read (``{"web_page": url, "full": true}``);
                                                   a longer document is paged with ``"part": N``
                                                   (default 12000).
  QAR_WEB_CACHE_DIR (optional)                  — directory for the web search/page cache.
  QAR_WEB_SEARCH_TTL_SECONDS (optional)         — cache TTL for search results.
  QAR_WEB_PAGE_TTL_SECONDS (optional)           — cache TTL for fetched pages.
  WEB_SEARCH_TIER (optional, LEGACY)            — model tier for the legacy provider-native
                                                   fold-in, only reached when no backend above is
                                                   configured (default balanced).
  QAR_EXPLAIN_ANSWER (optional)                 — "1"/"true" turns on the user-facing "Explain how
                                                   I got this" panel (see core/answer_explanation.py).
                                                   OFF by default. An ELIGIBLE turn (one that read,
                                                   executed, ran deep, answered on assembled context,
                                                   took more than one step, or searched the web) then
                                                   makes ONE extra cheap-tier call AFTER its answer
                                                   is already out and emits an "explanation" event.
                                                   Ineligible turns (plain small talk) emit nothing.
  QAR_EXPLAIN_TIER (optional)                   — tier that one extra call resolves to (default
                                                   "fast": it summarizes a turn that already
                                                   happened, it does not gate the turn's outcome).
  QAR_CONVERSATION_SEARCH (optional)           — set to "false"/"0"/"off" to disable searching
                                                   past Claude Code session transcripts during grep.
                                                   Enabled by default. Uses ~/.claude/sessions unless
                                                   QAR_CLAUDE_SESSIONS_DIR is set.
  QAR_CLAUDE_SESSIONS_DIR (optional)          — explicit path to the Claude Code sessions directory
                                                   to search (default: ~/.claude/sessions).
                                                   QAR reads this directory but never writes to it.
  QAR_CHAT_HISTORY_DIR (optional)             — directory where QAR writes its own chat session
                                                   history (default: ~/.quest-ai-runner/conversations).
                                                   Written by QAR; read back by QAR in future sessions,
                                                   and reopened by `chat --resume` (picker),
                                                   `chat --resume last|ID` or `chat --continue`.
                                                   See docs/chat.md.
  QAR_CHAT_LOG_FILE (optional)                 : `chat`: the file the full diagnostic log is written to
                                                   (default: logs/chat.log beside the conversations dir;
                                                   rotated and size capped). The transcript shows only
                                                   one short line per distinct warning. See docs/chat.md.
  QAR_QUEST_AUTO_MATCH (optional)             — `chat`: 0 makes "no quest" the default instead of
                                                   matching each message to a synced quest (a
                                                   person's own /quest choice still wins).
  QAR_QUEST_FOLDER_MAP (optional)             — JSON object mapping quest/goal id -> local folder,
                                                   e.g. {"quest_123": "/srv/corpus/some_quest"}. Each
                                                   entry's folder is kept in sync with its quest's
                                                   QUEST_SYNC.md EVERY poll scan (no task required),
                                                   plus opt-in around any task whose goal_id/quest_id
                                                   matches. See RunnerConfig.quest_folder_map.
  QAR_QUEST_FOLDER_SYNC_DIRECTION (optional)  — "pull" (default), "push", or "both" for the map
                                                   above. See RunnerConfig.quest_folder_sync_direction.
  QAR_CONTEXT_UPDATES (optional)              — "0"/"false" switches OFF the automated context
                                                   updates every autopilot pass carries (notes,
                                                   document comments, folder changes since a run
                                                   last looked). On by default. See
                                                   runner/context_updates.py.
  QAR_CONTEXT_UPDATES_STATE_PATH (optional)   — where the per (quest, source) "last looked" stamps
                                                   live (default: beside QAR_STATE_PATH).
  QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS (opt.)  — how far back a source looks for a quest nothing has
                                                   ever read (default 14).

channel-specific env vars (all optional; read by the `channel` subcommand only, see
docs/live-channels.md for the full picture including the OpenClaw operator checklist):
  QAR_CHANNEL_PROVIDER (optional)              — "openclaw" wires an OpenClawChannel from the vars
                                                   below. Unset (default) = no channel transport;
                                                   `channel` then logs an error and exits.
  QAR_OPENCLAW_TOKEN_FILE                      — REQUIRED for the openclaw provider: path to the
                                                   Gateway token file (never the token itself; see
                                                   adapters/openclaw_channel.py). Passed to the
                                                   subprocess as `--token-file <path>`.
  QAR_OPENCLAW_COMMAND (optional)              — the openclaw binary (default: "openclaw" on PATH).
  QAR_OPENCLAW_ARGS (optional)                 — extra args appended after "mcp serve", space-
                                                   separated.
  QAR_OPENCLAW_CWD (optional)                  — working dir for the spawned subprocess.
  QAR_OPENCLAW_CALL_TIMEOUT (optional, seconds) — per-call timeout for tools other than
                                                   events_wait (default 30).
  QAR_CHANNEL_NAME (optional)                  — the channel's own name / dedup-key prefix
                                                   (default "openclaw").
  QAR_CHANNEL_ALLOWED_SENDERS (optional)       — comma-separated sender ids this runner will act
                                                   on. EMPTY/unset = DENY ALL (fail closed) — see
                                                   RunnerConfig.channel_allowed_senders.
  QAR_CHANNEL_ACK_AFTER_SECONDS (optional)     — see RunnerConfig.channel_ack_after_seconds
                                                   (default 15).
  QAR_CHANNEL_PROGRESS_MIN_SECONDS (optional)  — see RunnerConfig.channel_progress_min_seconds
                                                   (default 20).
  QAR_CHANNEL_TURN_TIMEOUT_SECONDS (optional)  — see RunnerConfig.channel_turn_timeout_seconds
                                                   (default 900).
  QAR_CHANNEL_STATE_PATH (optional)            — dedup state file (default: in-memory only).

chat-specific env vars (all optional):
  QAR_REP_NAME (optional)                        — display name for the AI representative shown in
                                                   the interactive session (e.g. "Alex's AI").
                                                   Overridden by --rep on the command line.
  QAR_REP_PERSONA_FILE (optional)                — path to a persona/skill file whose content is
                                                   injected as rep_preamble into every chat turn.
                                                   Overridden by --persona-file on the command line.

A consumer that wants finer control imports the library and builds RunnerConfig itself instead.
"""
from __future__ import annotations

# Imports below stay importable from here for existing callers, although the code now lives in
# the cli_<role> modules re-exported at the bottom of this block.
import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import shutil

# fastembed (optional qdrant extra) pulls in huggingface_hub, which resets its own logger's level
# when it is first imported, so a setLevel call made before that import is undone by it. Its
# unauthenticated-request warning only stays quiet if the library reads this at import, so it is set
# before anything below can import it. A user's own value wins.
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

from .adapters import AnthropicProvider, ClaudeCliProvider, ClaudeConversationsAdapter, CompositeRetrievalAdapter, FilesAdapter, GeminiProvider, OpenAIProvider
from .adapters.openclaw_channel import OpenClawChannel, OpenClawChannelConfig
from .config import (ConfigFileError, RunnerConfig, apply_config_environment, apply_file_defaults,
                     load_quest_client, parse_decision_assignees, resolve_config_objects)
from .core.adapters import ModelProvider
from .pricing import estimate_bootstrap_cost, get_provider_and_model
from .runner.channel_runner import ChannelRunner
from .runner.poller import Poller

from .cli_config import (  # noqa: F401
    ASK_CLASSIFY_SYSTEM,
    ASSIGNED_GOAL_JUDGE_SYSTEM,
    _apply_channel_env,
    _ask_classifier,
    _channel_transport_from_env,
    _config_from_env,
    _goal_judge,
    _model_provider_from_env,
    _quest_model,
    _team_ids_from_env,
    ask_key_of,
    float_or_none,
)
from .cli_context import (  # noqa: F401
    _LARGE_CORPUS_FILES,
    _cmd_bootstrap,
    _cmd_context,
    _cmd_paste_context,
    _cmd_search_context,
    _count_indexable_files,
    select_card_ids_for_text,
)
from .cli_chat import (  # noqa: F401
    RESUME_PICK,
    _check_chat_prerequisites,
    _cmd_chat,
    resume_command,
)
from .cli_quest import (  # noqa: F401
    _QUEST_READ_EXACT,
    _QUEST_READ_PREFIXES,
    _cmd_create_goal,
    _cmd_reconcile_plan,
    _cmd_send,
    _cmd_sync_quest_folder,
    _parse_kw,
    _quest_callable_methods,
    _quest_method_is_read_only,
    _run_quest_command,
)
from .cli_cards import (  # noqa: F401
    run_cards_command,
)
from .cli_poll import (  # noqa: F401
    _cmd_channel,
    _cmd_poll,
)
from .cli_serve_files import (  # noqa: F401
    _cmd_serve_files,
)
from .cli_parser import (  # noqa: F401
    _build_parser,
)


def main(argv=None) -> int:
    # `context move-root|migrate-imports|store-info` maintain card stores; `context <quest_id>` (below)
    # reads a quest. They share the word "context", so the store commands are routed first.
    from .store_cli import is_store_command, run as run_store_command
    import sys as sys_module
    raw_argv = list(sys_module.argv[1:] if argv is None else argv)
    if is_store_command(raw_argv):
        return run_store_command(raw_argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Suppress verbose Gemini SDK logs
    logging.getLogger("google_genai.models").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # fastembed (optional qdrant extra) pulls in huggingface_hub, which warns about
    # unauthenticated requests even when nothing here uses a Hugging Face model directly. Its own
    # import resets this level, so HF_HUB_VERBOSITY (set at the top of this module) is what holds.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    log = logging.getLogger("quest-ai-runner")
    handlers = {
        "chat": _cmd_chat,
        "quest": lambda a, _log: _run_quest_command(a),
        "send": _cmd_send,
        "reconcile-plan": _cmd_reconcile_plan,
        "create-goal": _cmd_create_goal,
        "context": _cmd_context,
        "cards": run_cards_command,
        "sync-quest-folder": _cmd_sync_quest_folder,
        "bootstrap": _cmd_bootstrap,
        "paste-context": _cmd_paste_context,
        "search-context": _cmd_search_context,
        "channel": _cmd_channel,
        "serve-files": _cmd_serve_files,
    }
    # No subcommand (the legacy flat flags) and `poll` both run the poller.
    return handlers.get(args.command, _cmd_poll)(args, log)


if __name__ == "__main__":
    sys.exit(main())
