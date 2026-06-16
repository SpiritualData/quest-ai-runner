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
                                                   read/answer/deep (default haiku; raise to e.g.
                                                   sonnet when tasks are mostly real work/edits)
  QAR_STATE_PATH (optional)                      — signature store path (default: ./qar_state.json)
  QAR_POLL_INTERVAL (optional, seconds)          — loop cadence (default 900)
  QAR_RUNNER_LABEL (optional)                    — human-readable tag sent on the env heartbeat
  QAR_ENV_ID (optional)                          — which of the team's environments this runner is
                                                   (omit = the team's default env; set a distinct id
                                                   per runner when a team attaches SEVERAL)
  QAR_MAX_MEMORY_PERCENT (optional)              — pause new task pickup when system memory usage
                                                   exceeds this percent; resume when it recovers
  QAR_MIN_FREE_MEMORY_MB (optional)              — pause when available memory drops below this MB
  QAR_MAX_LOAD_PER_CORE (optional)               — pause when 1-min load average per CPU core
                                                   exceeds this (e.g. 2.0)
  QAR_RESOURCE_RESUME_MARGIN (optional, %)       — hysteresis: a tripped metric must clear its
                                                   limit by this percent before resuming (default 10)
  QAR_RESOURCE_CHECK_INTERVAL (optional, secs)   — re-check cadence while paused (default 30)
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
import logging
import os
import sys

from .adapters import AnthropicProvider, ClaudeCliProvider, FilesAdapter
from .config import RunnerConfig
from .core.adapters import ModelProvider
from .core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from .runner.poller import Poller


def _model_provider_from_env() -> ModelProvider:
    """Pick the model backend from env.

    ``QAR_MODEL_BACKEND`` forces a backend; absent it, we AUTO-SELECT: the keyless ``claude_cli``
    backend (planner/answer on the box's Claude Code subscription login) unless an
    ``ANTHROPIC_API_KEY`` is present, in which case the SDK-based ``AnthropicProvider`` is used.
    This means the runner works out of the box on a subscription login with NO API key.
    """
    backend = (os.getenv("QAR_MODEL_BACKEND") or "").strip().lower()
    if not backend:
        backend = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "claude_cli"
    if backend == "claude_cli":
        kwargs = {"claude_path": os.getenv("QAR_CLAUDE_PATH", "claude")}
        # Headless completions over a large corpus can take a while; let the consumer raise the
        # per-call wall-clock cap above the conservative default rather than failing the run.
        if os.getenv("QAR_ANSWER_TIMEOUT"):
            kwargs["timeout_seconds"] = float(os.environ["QAR_ANSWER_TIMEOUT"])
        return ClaudeCliProvider(**kwargs)
    return AnthropicProvider()


def _config_from_env() -> RunnerConfig:
    corpus = os.getenv("QAR_CORPUS_ROOT")
    retrieval = FilesAdapter(corpus) if corpus else None
    # QAR_DEEP_WORKING_DIR defaults to QAR_CORPUS_ROOT so only one env var is needed.
    deep_dir = os.getenv("QAR_DEEP_WORKING_DIR") or corpus
    deep_runner = None
    if deep_dir:
        deep_runner = SubprocessGoalRunner(SubprocessConfig(
            working_dir=deep_dir,
            claude_path=os.getenv("QAR_CLAUDE_PATH", "claude"),
        ))
    cfg = RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),
        team_id=os.getenv("QUEST_TEAM_ID", ""),
        retrieval=retrieval,
        model_provider=_model_provider_from_env(),
        deep_runner=deep_runner,
        corpus_root=corpus,
        runner_label=os.getenv("QAR_RUNNER_LABEL") or None,
        env_id=os.getenv("QAR_ENV_ID") or None,
    )
    if os.getenv("QAR_POLL_INTERVAL"):
        cfg.poll_interval_seconds = float(os.environ["QAR_POLL_INTERVAL"])
    # The planner step picks the next action (read/answer/deep/confirm). It defaults to a cheap
    # tier; a consumer whose tasks are mostly real work (e.g. code edits) can raise it so the
    # answer-vs-deep routing is decided by a more capable model.
    planner_tier = (os.getenv("QAR_PLANNER_TIER") or "").strip().lower()
    if planner_tier:
        cfg.orchestrator.planner_tier = planner_tier
    return cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="quest-ai-runner",
                                     description="Poll Quest for due AI tasks and execute them.")
    parser.add_argument("-v", "--verbose", action="store_true")
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

    # --- poll subcommand (and legacy flat flags, kept for back-compat) --------
    poll_p = sub.add_parser("poll", help="poll Quest for due tasks and run them")
    poll_p.add_argument("--once", action="store_true", help="one scan then exit (cron mode)")
    poll_p.add_argument("--check", action="store_true", help="validate config + key, then exit")

    # Legacy: flags directly on the root command (no subcommand given) stay working.
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("quest-ai-runner")

    # --- chat -----------------------------------------------------------------
    if args.command == "chat":
        from .interactive import start_interactive
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
            ack_model = ModelRegistry(provider).resolve_tier("haiku")
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

    # --- bootstrap ------------------------------------------------------------
    if args.command == "bootstrap":
        from .adapters.file_context_store import FileContextStore
        corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
        cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")
        provider = _model_provider_from_env()
        store = FileContextStore(cards_dir, repo_root=corpus)
        log.info("bootstrapping context store for %s", corpus)
        n = store.bootstrap(root=corpus, provider=provider)
        log.info("done: %d cards in %s", n, cards_dir)
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
