"""Console entry point for the poller (``quest-ai-runner``).

This is the thin CLI that runs the EXECUTOR lane. It builds a RunnerConfig from environment
variables (so a stranger's org can run it with no code) and starts the Poller in ``--once``
(cron) or loop (service) mode. NO consumer-specific defaults: every value comes from env.

Env it reads:
  QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID   — the Quest connection (key is qsk_...)
  QAR_CORPUS_ROOT                                — file root for the FilesAdapter (grounding);
                                                   also the default for QAR_DEEP_WORKING_DIR
  QAR_DEEP_WORKING_DIR (optional)               — working dir for the subprocess deep-runner;
                                                   defaults to QAR_CORPUS_ROOT when unset
  QAR_CLAUDE_PATH (optional)                     — the worker binary (default: claude on PATH)
  QAR_ANSWER_TIMEOUT (optional, seconds)         — per-call cap for the claude_cli planner/answer
                                                   backend (default 180; raise for large corpora)
  QAR_PLANNER_TIER (optional)                    — model tier for the planner step that picks
                                                   read/answer/deep (default balanced; use fast
                                                   to reduce cost, best for complex routing)
  QAR_STATE_PATH (optional)                      — signature store path (default: ./qar_state.json)
  QAR_POLL_INTERVAL (optional, seconds)          — loop cadence (default 900)
  QAR_RUNNER_LABEL (optional)                    — human-readable tag sent on the env heartbeat
  QAR_ENV_ID (optional)                          — which of the team's environments this runner is
                                                   (omit = the team's default env; set a distinct id
                                                   per runner when a team attaches SEVERAL)
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
  WEB_SEARCH_ENABLED (optional)                 — web search is ON by default and needs NO extra key:
                                                   it uses the model provider's native tool (Claude's
                                                   web_search / Gemini's Google Search grounding),
                                                   reusing the LLM key. Set to "false" to disable.
                                                   See docs/web-search.md.
  WEB_SEARCH_API_KEY (optional)                 — Tavily API key (tvly_...). When set, Tavily is used
                                                   instead of the native provider search. Get a key at
                                                   tavily.com (free tier: 500 searches/month).
  WEB_SEARCH_TIER (optional)                    — model tier for native web search (default balanced).
  WEB_SEARCH_MAX_RESULTS (optional)             — max results per web search call (default 5).
  QAR_CONVERSATION_SEARCH (optional)           — set to "false"/"0"/"off" to disable searching
                                                   past Claude Code session transcripts during grep.
                                                   Enabled by default. Uses ~/.claude/sessions unless
                                                   QAR_CLAUDE_SESSIONS_DIR is set.
  QAR_CLAUDE_SESSIONS_DIR (optional)          — explicit path to the Claude Code sessions directory
                                                   to search (default: ~/.claude/sessions).
                                                   QAR reads this directory but never writes to it.
  QAR_CHAT_HISTORY_DIR (optional)             — directory where QAR writes its own chat session
                                                   history (default: ~/.quest-ai-runner/conversations).
                                                   Written by QAR; read back by QAR in future sessions.
  QAR_QUEST_FOLDER_MAP (optional)             — JSON object mapping quest/goal id -> local folder,
                                                   e.g. {"quest_123": "/srv/corpus/some_quest"}. Each
                                                   entry's folder is kept in sync with its quest's
                                                   QUEST_SYNC.md EVERY poll scan (no task required),
                                                   plus opt-in around any task whose goal_id/quest_id
                                                   matches. See RunnerConfig.quest_folder_map.
  QAR_QUEST_FOLDER_SYNC_DIRECTION (optional)  — "pull" (default), "push", or "both" for the map
                                                   above. See RunnerConfig.quest_folder_sync_direction.

chat-specific env vars (all optional):
  QAR_REP_NAME (optional)                        — display name for the AI representative shown in
                                                   the interactive session (e.g. "Joshua's AI").
                                                   Overridden by --rep on the command line.
  QAR_REP_PERSONA_FILE (optional)                — path to a persona/skill file whose content is
                                                   injected as rep_preamble into every chat turn.
                                                   Overridden by --persona-file on the command line.

A consumer that wants finer control imports the library and builds RunnerConfig itself instead.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import shutil

from .adapters import AnthropicProvider, ClaudeCliProvider, ClaudeConversationsAdapter, CompositeRetrievalAdapter, FilesAdapter, GeminiProvider, OpenAIProvider, WebSearchAdapter
from .config import RunnerConfig
from .core.adapters import ModelProvider
from .core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from .pricing import estimate_bootstrap_cost, get_provider_and_model
from .runner.poller import Poller


def _model_provider_from_env() -> ModelProvider:
    """Pick the model backend from env.

    ``QAR_MODEL_BACKEND`` specifies: "anthropic", "claude_cli", "gemini", or "openai".
    Auto-select if not set: tries "openai" (OPENAI_API_KEY) → "gemini" (GOOGLE_API_KEY) →
    "anthropic" (ANTHROPIC_API_KEY) → "claude_cli" (keyless, subscription login).
    """
    backend = (os.getenv("QAR_MODEL_BACKEND") or "").strip().lower()
    if not backend:
        # Auto-select: openai > gemini > anthropic > claude_cli (keyless default)
        if os.getenv("OPENAI_API_KEY"):
            backend = "openai"
        elif os.getenv("GOOGLE_API_KEY"):
            backend = "gemini"
        elif os.getenv("ANTHROPIC_API_KEY"):
            backend = "anthropic"
        else:
            backend = "claude_cli"

    if backend == "openai":
        return OpenAIProvider()
    elif backend == "gemini":
        return GeminiProvider()
    elif backend == "claude_cli":
        kwargs = {"claude_path": os.getenv("QAR_CLAUDE_PATH", "claude")}
        # Headless completions over a large corpus can take a while; let the consumer raise the
        # per-call wall-clock cap above the conservative default rather than failing the run.
        if os.getenv("QAR_ANSWER_TIMEOUT"):
            kwargs["timeout_seconds"] = float(os.environ["QAR_ANSWER_TIMEOUT"])
        return ClaudeCliProvider(**kwargs)
    elif backend == "anthropic":
        return AnthropicProvider()
    else:
        raise ValueError(f"Unknown QAR_MODEL_BACKEND: {backend}. Use: openai, gemini, anthropic, or claude_cli")


def _web_search_adapter_from_env():
    """Build a WebSearchAdapter from env if WEB_SEARCH_ENABLED=true and a key is set.

    Env vars read:
      WEB_SEARCH_ENABLED    -- must be "true" (case-insensitive) to enable
      WEB_SEARCH_API_KEY    -- Tavily API key (tvly_...). Required when enabled.
      WEB_SEARCH_MAX_RESULTS -- max results per search (default 5)
    """
    enabled = (os.getenv("WEB_SEARCH_ENABLED") or "").strip().lower() == "true"
    if not enabled:
        return None
    api_key = (os.getenv("WEB_SEARCH_API_KEY") or "").strip()
    if not api_key:
        import logging
        logging.getLogger("quest-ai-runner").warning(
            "WEB_SEARCH_ENABLED=true but WEB_SEARCH_API_KEY is not set; web search disabled"
        )
        return None
    max_results = 5
    try:
        max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
    except ValueError:
        pass
    return WebSearchAdapter(api_key=api_key, max_results=max_results)


def _config_from_env() -> RunnerConfig:
    corpus = os.getenv("QAR_CORPUS_ROOT")
    retrieval = FilesAdapter(corpus) if corpus else None

    # Optionally add live web search to the retrieval stack.
    # WEB_SEARCH_ENABLED=true + WEB_SEARCH_API_KEY=tvly_... enables it.
    web_adapter = _web_search_adapter_from_env()
    if web_adapter is not None:
        if retrieval is not None:
            retrieval = CompositeRetrievalAdapter([retrieval, web_adapter])
        else:
            retrieval = web_adapter

    # Add conversation search unless explicitly disabled. Two separate adapters:
    # 1. Claude Code sessions (~/.claude/sessions) — read-only; written by Claude Code, not QAR.
    # 2. QAR's own chat history (~/.quest-ai-runner/conversations) — read+write by QAR.
    _conv_search = (os.getenv("QAR_CONVERSATION_SEARCH") or "").strip().lower()
    if _conv_search not in ("false", "0", "off", "no"):
        conv_adapters = []
        # Claude Code sessions (read-only for QAR)
        sessions_dir = os.getenv("QAR_CLAUDE_SESSIONS_DIR") or None
        try:
            conv_adapters.append(ClaudeConversationsAdapter(
                corpus_root=corpus,
                sessions_dir=sessions_dir,
            ))
        except Exception:
            pass
        # QAR's own chat history (read+write by QAR)
        chat_history_dir = os.getenv("QAR_CHAT_HISTORY_DIR") or str(
            Path.home() / ".quest-ai-runner" / "conversations"
        )
        if chat_history_dir:
            try:
                conv_adapters.append(ClaudeConversationsAdapter(sessions_dir=chat_history_dir))
            except Exception:
                pass
        for adapter in conv_adapters:
            if retrieval is not None:
                retrieval = CompositeRetrievalAdapter([retrieval, adapter])
            else:
                retrieval = adapter

    # QAR_DEEP_WORKING_DIR defaults to QAR_CORPUS_ROOT so only one env var is needed.
    deep_dir = os.getenv("QAR_DEEP_WORKING_DIR") or corpus
    deep_runner = None
    if deep_dir:
        deep_runner = SubprocessGoalRunner(SubprocessConfig(
            working_dir=deep_dir,
            claude_path=os.getenv("QAR_CLAUDE_PATH", "claude"),
        ))
    # Allow model tier overrides via env vars: QAR_MODEL_FAST, QAR_MODEL_BALANCED, QAR_MODEL_QUALITY, QAR_MODEL_BEST
    model_fallback = {}
    for tier in ("fast", "balanced", "quality", "best"):
        env_key = f"QAR_MODEL_{tier.upper()}"
        if os.getenv(env_key):
            model_fallback[tier] = os.getenv(env_key)

    cfg = RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),
        team_id=os.getenv("QUEST_TEAM_ID", ""),
        retrieval=retrieval,
        model_provider=_model_provider_from_env(),
        model_fallback=model_fallback or None,
        deep_runner=deep_runner,
        corpus_root=corpus,
        runner_label=os.getenv("QAR_RUNNER_LABEL") or None,
        env_id=os.getenv("QAR_ENV_ID") or None,
    )
    if os.getenv("QAR_POLL_INTERVAL"):
        cfg.poll_interval_seconds = float(os.environ["QAR_POLL_INTERVAL"])

    # --- quest <-> local folder sync (opt-in; see RunnerConfig.quest_folder_map) ---------------
    # QAR_QUEST_FOLDER_MAP: a JSON object mapping quest/goal id -> local folder, e.g.
    #   QAR_QUEST_FOLDER_MAP={"quest_1625d9f47a06": "/srv/corpus/concept_ai_interface_quest"}
    # Every entry is kept in sync with its quest EVERY scan (Poller.run_once, so the standing
    # poll/systemd process keeps it current with no task required) plus, opt-in, right before/after
    # any task whose goal_id/quest_id matches an entry.
    if os.getenv("QAR_QUEST_FOLDER_MAP"):
        try:
            parsed = json.loads(os.environ["QAR_QUEST_FOLDER_MAP"])
            if isinstance(parsed, dict):
                cfg.quest_folder_map = {str(k): str(v) for k, v in parsed.items()}
            else:
                logging.getLogger("quest-ai-runner").warning(
                    "QAR_QUEST_FOLDER_MAP must be a JSON object; ignoring")
        except (ValueError, TypeError) as e:
            logging.getLogger("quest-ai-runner").warning(
                "QAR_QUEST_FOLDER_MAP is not valid JSON (%s); ignoring", e)
    if os.getenv("QAR_QUEST_FOLDER_SYNC_DIRECTION"):
        cfg.quest_folder_sync_direction = os.environ["QAR_QUEST_FOLDER_SYNC_DIRECTION"].strip().lower()

    # --- fast lane for interactive context-requests (D2 revised: presence-aware push) -----------
    # QAR_WAIT_CHANNEL: "0"/"false"/"off" disables the long-poll wait channel, falling back to a
    # short interval poll (QAR_CONTEXT_POLL_SECONDS). Default: enabled.
    _wait_channel = (os.getenv("QAR_WAIT_CHANNEL") or "").strip().lower()
    if _wait_channel in ("0", "false", "off", "no"):
        cfg.wait_channel_enabled = False
    elif _wait_channel in ("1", "true", "on", "yes"):
        cfg.wait_channel_enabled = True
    if os.getenv("QAR_CONTEXT_POLL_SECONDS"):
        try:
            cfg.context_poll_seconds = float(os.environ["QAR_CONTEXT_POLL_SECONDS"])
        except ValueError:
            pass
    if os.getenv("QAR_WAIT_TIMEOUT_SECONDS"):
        try:
            cfg.wait_timeout_seconds = float(os.environ["QAR_WAIT_TIMEOUT_SECONDS"])
        except ValueError:
            pass
    # The planner step picks the next action (read/answer/deep/confirm). It defaults to a cheap
    # tier; a consumer whose tasks are mostly real work (e.g. code edits) can raise it so the
    # answer-vs-deep routing is decided by a more capable model.
    planner_tier = (os.getenv("QAR_PLANNER_TIER") or "").strip().lower()
    if planner_tier:
        cfg.orchestrator.planner_tier = planner_tier

    # --- Deep goal loop tuning (our own /goal replacement) -------------------------------------
    # The deep worker is Claude Code, so it can ONLY run Claude models. QAR_DEEP_MODELS is the
    # model LADDER the goal loop escalates through on a not-met goal, weak -> strong, e.g.
    # "claude-sonnet-4-6,claude-opus-4-8" or the bare aliases "sonnet,opus". Give REAL Claude
    # model ids/aliases here, not semantic tier names ("fast"/"balanced"/"quality"/"best") -- those
    # are tier names the ModelRegistry resolves per-DEPLOYMENT, which on a Gemini/OpenAI deployment
    # resolve to non-Claude ids the worker cannot run, silently making the ladder inert (see
    # HANDS_FREE_QUEST_AI_DESIGN.md section 2, point 4). Left UNSET (the default): the ladder is
    # NOT hardcoded here -- Orchestrator._deep_models resolves one generically at run time (pins to
    # an already-Claude-runnable fallback model, then tries to extend it with any Claude id found by
    # resolving the "quality"/"best" tiers, e.g. from QAR_MODEL_BEST), and logs what it found.
    deep_models_env = (os.getenv("QAR_DEEP_MODELS") or "").strip()
    if deep_models_env:
        cfg.orchestrator.deep_model_ladder = (
            [m.strip() for m in deep_models_env.split(",") if m.strip()] or None
        )
    # Overall TOKEN BUDGET for one turn's deep goal loop (worker tokens summed across attempts).
    # Operator-tunable; replaces a fixed attempt count as the primary stop.
    if os.getenv("QAR_GOAL_TOKEN_BUDGET"):
        try:
            cfg.orchestrator.deep_goal_token_budget = int(os.environ["QAR_GOAL_TOKEN_BUDGET"])
        except ValueError:
            pass
    # Hard safety cap on attempts (the budget is the primary control; this just bounds runaway).
    if os.getenv("QAR_GOAL_MAX_ATTEMPTS"):
        try:
            cfg.orchestrator.deep_goal_max_iterations = int(os.environ["QAR_GOAL_MAX_ATTEMPTS"])
        except ValueError:
            pass
    # --- Async post-deep context-card updater (prepare for the future) -------------------------
    # ON by default; set QAR_ASYNC_CARD_UPDATE=0/false/off to disable (then deep is unchanged and no
    # future-context section is appended to deep briefs). Inert anyway without a card-update store.
    _acu = (os.getenv("QAR_ASYNC_CARD_UPDATE") or "").strip().lower()
    if _acu in ("0", "false", "off", "no"):
        cfg.orchestrator.async_card_update = False
    elif _acu in ("1", "true", "on", "yes"):
        cfg.orchestrator.async_card_update = True
    # --- Minimal-intervention overseer (the simulated-conscious judge) -------------------------
    # QAR_OVERSEER=1/true enables it (library default: off). A high-quality model reads a tiny
    # capped digest of the run and almost always stays silent; occasionally it sends ONE signal
    # (redirect / answer_now / escalate_deep / escalate_human). Consults are non-blocking (a
    # background thread polled without waiting), pre-filtered by a free heuristic gate, and capped
    # at QAR_OVERSEER_MAX_SIGNALS per run, so the risk coverage costs at most a few small calls.
    _ovr = (os.getenv("QAR_OVERSEER") or "").strip().lower()
    if _ovr in ("1", "true", "on", "yes"):
        cfg.orchestrator.overseer = True
    elif _ovr in ("0", "false", "off", "no"):
        cfg.orchestrator.overseer = False
    # QAR_OVERSEER_TIER: the judge's model tier (default "best": judgment is where the expensive
    # model pays; the digest keeps its token cost tiny regardless).
    if (os.getenv("QAR_OVERSEER_TIER") or "").strip():
        cfg.orchestrator.overseer_tier = os.environ["QAR_OVERSEER_TIER"].strip().lower()
    if os.getenv("QAR_OVERSEER_MAX_SIGNALS"):
        try:
            cfg.orchestrator.overseer_max_signals = int(os.environ["QAR_OVERSEER_MAX_SIGNALS"])
        except ValueError:
            pass
    # QAR_VERIFY_TIER: the goal/claims verification judge's tier (default "best" — this one small
    # hard-capped call gates done vs needs_you/failed and claim honesty). Set "balanced" to cheapen.
    if (os.getenv("QAR_VERIFY_TIER") or "").strip():
        cfg.orchestrator.verify_tier = os.environ["QAR_VERIFY_TIER"].strip().lower()
    # --- Warm recent-turn context fallback (see core/recent_context.py) ------------------------
    # ON by default (library default: True). QAR_RECENT_CONTEXT=0/false disables it entirely (no
    # store is wired by build_orchestrator's resolve_recent_context_store, so recent-turn cards are
    # never consulted or recorded). QAR_RECENT_CONTEXT_MAX_CARDS caps how many recent-turn cards
    # filter_relevant may let through in one turn (default 6; these are ADDITIONAL to whatever the
    # fresh assembly finds).
    _rc = (os.getenv("QAR_RECENT_CONTEXT") or "").strip().lower()
    if _rc in ("0", "false", "off", "no"):
        cfg.orchestrator.recent_context_enabled = False
    elif _rc in ("1", "true", "on", "yes"):
        cfg.orchestrator.recent_context_enabled = True
    if os.getenv("QAR_RECENT_CONTEXT_MAX_CARDS"):
        try:
            cfg.orchestrator.recent_context_max_cards = int(os.environ["QAR_RECENT_CONTEXT_MAX_CARDS"])
        except ValueError:
            pass
    # QAR_RECENT_CONTEXT_GLOBAL: the recent-context store's "global" scope (everything recently
    # selected anywhere, not just this conversation/quest) is ON by default. "0"/"false" disables
    # ONLY cross-conversation/cross-quest memory; conv- and quest-scoped warm context (and the
    # QAR_RECENT_CONTEXT master switch above) are unaffected.
    _rcg = (os.getenv("QAR_RECENT_CONTEXT_GLOBAL") or "").strip().lower()
    if _rcg in ("0", "false", "off", "no"):
        cfg.orchestrator.recent_context_global_enabled = False
    elif _rcg in ("1", "true", "on", "yes"):
        cfg.orchestrator.recent_context_global_enabled = True
    return cfg


def _check_chat_prerequisites(env=None, which=shutil.which) -> List[str]:
    """Validate what `chat` needs before it can run, without opening the TUI.

    Returns a list of human-readable problems; an empty list means chat is ready to start.
    Checks: a model provider must be reachable (an API key env var, or the claude CLI on PATH
    for the keyless default), and the corpus/context store path, if configured, must exist.
    A pure function (env/which injectable) so it is testable offline.
    """
    env = env if env is not None else os.environ
    problems: List[str] = []

    backend = (env.get("QAR_MODEL_BACKEND") or "").strip().lower()
    has_key = bool(
        env.get("OPENAI_API_KEY") or env.get("GOOGLE_API_KEY") or env.get("ANTHROPIC_API_KEY")
    )
    if not backend or backend == "claude_cli":
        if not has_key and which("claude") is None:
            problems.append(
                "no model provider available: set ANTHROPIC_API_KEY (or OPENAI_API_KEY / "
                "GOOGLE_API_KEY), or install the claude CLI and log in"
            )

    corpus = env.get("QAR_CORPUS_ROOT")
    if corpus and not os.path.isdir(corpus):
        problems.append(f"QAR_CORPUS_ROOT is set but does not exist: {corpus}")

    cards_dir = env.get("QAR_CONTEXT_CARDS_DIR")
    if cards_dir and not os.path.isdir(cards_dir):
        problems.append(f"QAR_CONTEXT_CARDS_DIR is set but does not exist: {cards_dir}")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="quest-ai-runner",
                                     description="Poll Quest for due AI tasks and execute them.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="increase verbosity: -v for info, -vv for debug")
    sub = parser.add_subparsers(dest="command")

    # --- chat subcommand: interactive attended session ------------------------
    chat_p = sub.add_parser("chat", help="start an interactive session with the brain")
    chat_p.add_argument("--rep", default=None, metavar="NAME",
                        help="AI representative display name shown in the session "
                             "(default: QAR_REP_NAME env var, else 'AI')")
    chat_p.add_argument("--persona-file", default=None, metavar="PATH",
                        help="path to a persona/skill file injected into every turn "
                             "(default: QAR_REP_PERSONA_FILE env var)")
    chat_p.add_argument("--goal-id", default=None, help="attach session to this Quest goal id")
    chat_p.add_argument("--check", action="store_true",
                        help="validate chat prerequisites (model provider, context store) and exit, "
                             "without opening the terminal UI")

    # --- send subcommand: enqueue a new AI task -------------------------------
    send_p = sub.add_parser("send", help="enqueue a new AI task and print its id")
    send_p.add_argument("text", help="the task instruction")
    send_p.add_argument("--team-id", default=None,
                        help="route to this team (default: QUEST_TEAM_ID env var)")
    send_p.add_argument("--goal-id", default=None, help="attach to this goal id")
    send_p.add_argument("--at", dest="scheduled_at", default=None,
                        help="ISO-8601 UTC datetime to schedule (omit = run at next poll)")

    # --- bootstrap subcommand: build/refresh the context card store ----------
    boot_p = sub.add_parser("bootstrap", help="build or refresh the context card store for the corpus")
    boot_p.add_argument("--corpus", default=None, metavar="PATH",
                        help="corpus root (default: QAR_CORPUS_ROOT env var)")
    boot_p.add_argument("--cards-dir", default=None, metavar="PATH",
                        help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    boot_p.add_argument("--force", action="store_true",
                        help="delete all existing cards and bootstrap from scratch (forces re-index when algorithm changes)")
    boot_p.add_argument("--dry-run", action="store_true",
                        help="estimate tokens, cost, and time without running bootstrap")

    # --- paste-context subcommand: save context to a card -----------------------
    paste_p = sub.add_parser("paste-context", help="save context to a context card (from args or stdin)")
    paste_p.add_argument("context", nargs="?", default=None,
                         help="context text (if not provided, reads from stdin)")
    paste_p.add_argument("--card-id", default=None,
                         help="card id or key to save under (default: auto-generate from content)")
    paste_p.add_argument("--cards-dir", default=None, metavar="PATH",
                         help="cards directory (default: <corpus>/.quest-context or QAR_CONTEXT_CARDS_DIR)")
    paste_p.add_argument("--corpus", default=None, metavar="PATH",
                         help="corpus root (default: QAR_CORPUS_ROOT env var)")
    paste_p.add_argument("--goal-id", default=None,
                         help="optional: quest goal id for metadata")

    # --- search-context subcommand: show what context cards a query would surface --
    sc_p = sub.add_parser("search-context", help="show which context cards a query selects")
    sc_p.add_argument("query", help="the input text to search context for")
    sc_p.add_argument("--corpus", default=None, metavar="PATH",
                      help="corpus root (default: QAR_CORPUS_ROOT env var or cwd)")
    sc_p.add_argument("--cards-dir", default=None, metavar="PATH",
                      help="cards directory (default: <corpus>/.quest-context)")
    sc_p.add_argument("--no-llm", action="store_true",
                      help="skip LLM relevance filter, show raw IDF results only")

    # --- sync-quest-folder subcommand: pull/push a quest <-> a local folder --
    sync_p = sub.add_parser("sync-quest-folder",
                            help="sync a Quest quest's state/notes with a local folder's QUEST_SYNC.md")
    sync_p.add_argument("quest_id", help="the quest id to sync (e.g. quest_1625d9f47a06)")
    sync_p.add_argument("folder", help="local folder holding (or to hold) QUEST_SYNC.md")
    sync_p.add_argument("--direction", choices=["pull", "push", "both"], default="pull",
                        help="pull: Quest -> folder (default); push: folder -> Quest; "
                             "both: pull then push")

    # --- poll subcommand (and legacy flat flags, kept for back-compat) --------
    poll_p = sub.add_parser("poll", help="poll Quest for due tasks and run them")
    poll_p.add_argument("--once", action="store_true", help="one scan then exit (cron mode)")
    poll_p.add_argument("--check", action="store_true", help="validate config + key, then exit")

    # Legacy: flags directly on the root command (no subcommand given) stay working; also
    # documented on `poll` above. Kept visible (not argparse.SUPPRESS) so `-h` shows them.
    parser.add_argument("--once", action="store_true",
                        help="poll mode: one scan then exit (cron mode); same as 'poll --once'")
    parser.add_argument("--check", action="store_true",
                        help="poll mode: validate config + key, then exit; same as 'poll --check'")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Suppress verbose Gemini SDK logs
    logging.getLogger("google_genai.models").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("quest-ai-runner")

    # --- chat -----------------------------------------------------------------
    if args.command == "chat":
        if getattr(args, "check", False):
            chat_problems = _check_chat_prerequisites()
            if chat_problems:
                for p in chat_problems:
                    log.error("chat not ready: %s", p)
                return 1
            log.info("chat prerequisites OK")
            return 0

        cfg = _config_from_env()
        # chat only needs a model provider — Quest credentials and a retrieval
        # adapter are optional (no corpus = no grounding, but still works).
        _skip = {"quest", "retrieval adapter", "team_id"}
        problems = [p for p in cfg.validate()
                    if not any(kw in p for kw in _skip)]
        if problems:
            for p in problems:
                log.error("config error: %s", p)
            return 1
        rep_name = args.rep or os.getenv("QAR_REP_NAME") or "Assistant"
        persona = None
        persona_path = args.persona_file or os.getenv("QAR_REP_PERSONA_FILE")
        if persona_path:
            try:
                with open(persona_path) as fh:
                    persona = fh.read()
            except OSError as e:
                log.error("could not read persona file %r: %s", persona_path, e)
                return 1

        # Try Textual UI first (smooth 120 FPS terminal), fall back to ANSI
        try:
            from .textual_session import is_textual_available, start_textual_interactive
            if is_textual_available():
                log.debug("using Textual UI for chat session")
                start_textual_interactive(cfg, rep_name=rep_name, persona=persona, goal_id=args.goal_id, verbosity=args.verbose)
                return 0
        except Exception as e:
            log.debug("Textual UI failed, falling back to ANSI: %s", e)

        # Fallback to original ANSI-based interactive session
        from .interactive import start_interactive
        start_interactive(cfg, rep_name=rep_name, persona=persona, goal_id=args.goal_id)
        return 0

    # --- send -----------------------------------------------------------------
    if args.command == "send":
        from .runner.quest_client import QuestClient, QuestApiError, QuestNotConfigured
        base_url = os.getenv("QUEST_BASE_URL", "")
        api_key = os.getenv("QUEST_API_KEY", "")
        team_id = args.team_id or os.getenv("QUEST_TEAM_ID", "")
        if not base_url or not api_key:
            log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
            return 1
        client = QuestClient(base_url, api_key, team_id=team_id)
        try:
            task = client.create_task(
                args.text,
                team_id=args.team_id,
                goal_id=args.goal_id,
                scheduled_at=args.scheduled_at,
            )
        except (QuestApiError, QuestNotConfigured) as e:
            log.error("failed to enqueue task: %s", e)
            return 1
        # Immediate ack: fire a cheap one-sentence restatement so the user sees feedback
        # right away, then exit.  The queued task runs in the background via the poller.
        try:
            provider = _model_provider_from_env()
            from .core.model_registry import ModelRegistry
            ack_model = ModelRegistry(provider).resolve_tier("fast")
            ack_prompt = (
                "Write ONE sentence (max 20 words) that restates the following "
                "request in your own words and says you are looking into it. "
                "Do NOT use em dashes (--). Be natural and brief.\n\n"
                f"Request: {args.text[:300]}"
            )
            ack = provider.answer([{"role": "user", "content": ack_prompt}], model=ack_model)
            if ack and ack.strip():
                print(ack.strip())
                return 0
        except Exception:  # noqa: BLE001 — ack failure is non-fatal
            pass
        task_id = task.get("id") or task.get("task_id") or "?"
        print(f"Queued — {args.text[:80]}  ({task_id})")
        return 0

    # --- sync-quest-folder ------------------------------------------------------
    if args.command == "sync-quest-folder":
        from .runner.quest_client import QuestClient
        from .runner.quest_folder_sync import QuestFolderSyncError, sync_quest_folder
        base_url = os.getenv("QUEST_BASE_URL", "")
        api_key = os.getenv("QUEST_API_KEY", "")
        team_id = os.getenv("QUEST_TEAM_ID", "")
        if not base_url or not api_key:
            log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
            return 1
        client = QuestClient(base_url, api_key, team_id=team_id)
        try:
            result = sync_quest_folder(client, args.quest_id, args.folder, direction=args.direction)
        except QuestFolderSyncError as e:
            log.error("sync failed: %s", e)
            return 1
        parts = []
        if result.pulled:
            parts.append(f"pulled {result.notes_pulled} note(s)")
        if result.pushed:
            parts.append(f"pushed {result.notes_pushed} note(s)")
        print(f"Synced quest {args.quest_id} -> {result.sync_path} ({', '.join(parts)})")
        return 0

    # --- bootstrap ------------------------------------------------------------
    if args.command == "bootstrap":
        from .adapters.file_context_store import FileContextStore
        from .adapters._walk import effective_skip_dirs, prune_dirnames
        import shutil
        from pathlib import Path

        corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
        cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")

        # Dry-run mode: run bootstrap without writing cards, track tokens
        if args.dry_run:
            from .adapters.dryrun_provider import DryRunProvider
            from .adapters.file_context_store import FileContextStore
            from .adapters._walk import effective_skip_dirs, prune_dirnames

            # Count source files and estimate areas
            corpus_path = Path(corpus)
            skip_dirs = effective_skip_dirs(corpus_path)
            file_count = 0

            for dirpath, dirnames, filenames in os.walk(corpus_path):
                prune_dirnames(dirnames, current=Path(dirpath).resolve(), base_skip=skip_dirs)
                for fname in filenames:
                    if Path(fname).suffix in {
                        ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
                        ".java", ".rb", ".md", ".sh", ".yaml", ".yml", ".toml", ".json",
                        ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
                        ".html", ".css", ".scss", ".less", ".txt", ".rst",
                    }:
                        fpath = Path(dirpath) / fname
                        try:
                            if fpath.stat().st_size <= 512 * 1024:  # 512KB max
                                file_count += 1
                        except OSError:
                            pass

            # Run bootstrap with DryRunProvider to track actual tokens
            provider = _model_provider_from_env()
            dryrun_provider = DryRunProvider(provider)

            store = FileContextStore(cards_dir, repo_root=corpus, dry_run=True)
            store.bootstrap(root=corpus, provider=dryrun_provider)

            # Get token counts from the provider
            tokens_in = dryrun_provider.tokens_in
            tokens_out = dryrun_provider.tokens_out
            total_tokens = tokens_in + tokens_out

            # Estimate number of cards using heuristic: ~50 files per card
            estimated_cards = max(1, file_count // 50)

            # Estimate TF-DF-IDF sampling ratio: ~10% of files sampled
            sampled_files = max(10, file_count // 10)
            file_tokens_saved = (file_count - sampled_files) * 20  # ~20 chars per path
            savings_pct = 75  # typical savings with TF-DF-IDF

            # Cost estimate using actual provider/model pricing
            cost, prov, model = estimate_bootstrap_cost(tokens_in)

            # Time estimate: account for LLM call latency
            llm_calls = 2  # Stage 1 + Stage 2
            call_overhead = llm_calls * 4  # ~4s per call
            token_processing = max(10, (total_tokens // 100))  # ~100 tokens/sec
            time_estimate = call_overhead + token_processing

            corpus_abs = str(corpus_path.resolve())
            print()
            print("DRY RUN: Bootstrap Cost Estimate")
            print("=" * 50)
            print(f"Corpus: {corpus_abs}")
            print(f"Source files: {file_count:,}")
            print(f"Estimated areas: {max(5, file_count // 50)}")
            print()
            print("Tokens (with TF-DF-IDF sampling):")
            print(f"  Input tokens: {tokens_in:,}")
            print(f"  Output tokens: {tokens_out:,}")
            print(f"  Vs. full list: {file_tokens_saved:,} tokens saved ({savings_pct}%)")
            print()
            print(f"Provider: {prov}")
            print(f"Model: {model}")
            print()
            print("Cost & Time:")
            print(f"  Estimated cost: ${cost:.4f}")
            print(f"  Estimated time: ~{time_estimate}s (~{time_estimate // 60}m)")
            print()
            print("Run without --dry-run to bootstrap.")
            print()
            return 0

        # Force mode: delete all cards and bootstrap from scratch
        if args.force:
            if os.path.exists(cards_dir):
                log.info("deleting existing cards directory: %s", cards_dir)
                shutil.rmtree(cards_dir)
                log.info("cards deleted")

        import time

        provider = _model_provider_from_env()
        store = FileContextStore(cards_dir, repo_root=corpus)
        log.info("bootstrapping context store for %s", corpus)

        # Resolve a model from the provider
        from .core.model_registry import ModelRegistry
        registry = ModelRegistry(provider)
        model = registry.resolve_tier("balanced")

        start_time = time.time()
        n = store.bootstrap(root=corpus, provider=provider, model=model)
        elapsed_time = time.time() - start_time

        log.info("done: %d cards in %s", n, cards_dir)

        # Display bootstrap summary in nice format
        corpus_abs = str(Path(corpus).resolve())
        tokens_in = getattr(provider, "tokens_in", 0)
        tokens_out = getattr(provider, "tokens_out", 0)
        cost, prov, model = estimate_bootstrap_cost(tokens_in)

        print()
        print("Bootstrap Complete")
        print("=" * 50)
        print(f"Corpus: {corpus_abs}")
        print(f"Cards created: {n}")
        print()
        if tokens_in > 0:
            print("Tokens used:")
            print(f"  Input: {tokens_in:,}")
            print(f"  Output: {tokens_out:,}")
            print()
        print(f"Provider: {prov}")
        print(f"Model: {model}")
        print()
        if tokens_in > 0:
            print(f"Cost: ${cost:.4f}")
        print(f"Time: {elapsed_time:.0f}s (~{int(elapsed_time // 60)}m)")
        print()

        return 0

    # --- paste-context: save context from args or stdin to a card ---------------
    if args.command == "paste-context":
        import sys
        import hashlib
        import json
        from pathlib import Path

        corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
        cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")

        # Read context from args or stdin
        if args.context:
            context_text = args.context
        else:
            try:
                context_text = sys.stdin.read()
            except KeyboardInterrupt:
                log.error("interrupted")
                return 1
            except Exception as e:  # noqa: BLE001
                log.error("failed to read stdin: %s", e)
                return 1

        if not context_text.strip():
            log.error("no context provided")
            return 1

        # Determine card id
        if args.card_id:
            card_id = args.card_id
        else:
            # Auto-generate from content hash
            h = hashlib.sha256(context_text.encode("utf-8")).hexdigest()[:8]
            card_id = f"pasted-{h}"

        # Build the card
        from datetime import datetime, timezone
        card = {
            "id": card_id,
            "keywords": ["pasted", "context"],
            "summary": context_text.split('\n')[0][:100],  # First line or 100 chars
            "content": context_text,
            "files": [],
            "conventions": [],
            "provenance": {
                "created_by_task": "paste-context",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            "usage_count": 0,
            "last_outcome": "unknown",
        }
        if args.goal_id:
            card["goal_id"] = args.goal_id

        # Ensure cards directory exists
        Path(cards_dir).mkdir(parents=True, exist_ok=True)

        # Write card atomically
        card_path = Path(cards_dir) / f"{card_id}.json"
        try:
            tmp_fd, tmp_path = __import__("tempfile").mkstemp(
                dir=str(cards_dir),
                prefix=".tmp_",
                suffix=".json"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(card, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp_path, str(card_path))
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:  # noqa: BLE001
            log.error("failed to write card: %s", e)
            return 1

        print(f"Saved to {card_path}")
        print(f"Card ID: {card_id}")
        return 0

    # --- search-context: show which cards a query selects ---------------------
    if args.command == "search-context":
        import os as _os
        corpus = getattr(args, "corpus", None) or _os.getenv("QAR_CORPUS_ROOT") or _os.getcwd()
        cards_dir = getattr(args, "cards_dir", None) or _os.getenv("QAR_CONTEXT_CARDS_DIR") or _os.path.join(corpus, ".quest-context")

        from .adapters.file_context_store import FileContextStore
        provider = None
        filter_model = None
        if not getattr(args, "no_llm", False):
            from .config import build_orchestrator
            cfg = _config_from_env()
            build_orchestrator(cfg)  # wraps cfg.model_provider with MultiProvider
            provider = cfg.model_provider
            if provider is not None:
                from .core.model_registry import ModelRegistry
                filter_model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")

        store = FileContextStore(cards_dir, repo_root=corpus, auto_bootstrap=False,
                                 provider=provider, model=filter_model)
        result = store.assemble(args.query)

        if not result.card_ids:
            print("No context cards matched.")
            return 0

        mode = "IDF only" if getattr(args, "no_llm", False) else "IDF + LLM filter"
        print(f"Query: {args.query}")
        print(f"Found {len(result.card_ids)} card(s)  [{mode}]")
        print()
        for i, m in enumerate(result.card_metadata, 1):
            title = m.get("title") or m["id"]
            score_pct = int(m["relevance_score"] * 100)
            fc = m["file_count"]
            print(f"  {i}. {title}  ({score_pct}% match, {fc} file{'s' if fc != 1 else ''})")
            for f in m.get("files", []):
                print(f"       {f}")
            print()
        if result.sources:
            all_items = []
            for src in result.sources:
                all_items.extend(src.get("items", []))
            if all_items:
                print(f"Top sources ({len(all_items)} total):")
                for f in all_items[:8]:
                    print(f"  {f}")
                if len(all_items) > 8:
                    print(f"  ... +{len(all_items) - 8} more")
        return 0

    # --- poll (default when no subcommand given) ------------------------------
    cfg = _config_from_env()
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.info("config incomplete: %s", p)
        return 0

    poller = Poller(cfg, state_path=os.getenv("QAR_STATE_PATH", "qar_state.json"))

    once = args.once or (args.command == "poll" and getattr(args, "once", False))
    check = args.check or (args.command == "poll" and getattr(args, "check", False))

    if check:
        try:
            who = poller.client.whoami()
            log.info("key OK: %s", who)
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("key check failed: %s", e)
            return 1

    if once:
        handled = poller.run_once()
        log.info("handled %d task(s)", len(handled))
        return 0

    poller.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
