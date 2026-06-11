"""Console entry point for the poller (``quest-ai-runner``).

This is the thin CLI that runs the EXECUTOR lane. It builds a RunnerConfig from environment
variables (so a stranger's org can run it with no code) and starts the Poller in ``--once``
(cron) or loop (service) mode. NO consumer-specific defaults: every value comes from env.

Env it reads:
  QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID   — the Quest connection (key is qsk_...)
  QAR_CORPUS_ROOT                                — file root for the FilesAdapter (grounding)
  QAR_DEEP_WORKING_DIR                           — working dir for the subprocess deep-runner
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
  ANTHROPIC_API_KEY (optional)                   — only for the "anthropic" backend (per-token
                                                   billing). NOT needed for the keyless claude_cli
                                                   backend, which runs on Claude Code's subscription.

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
    deep_dir = os.getenv("QAR_DEEP_WORKING_DIR")
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
    parser.add_argument("--once", action="store_true", help="one scan then exit (cron mode)")
    parser.add_argument("--check", action="store_true", help="validate config + key, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = _config_from_env()
    problems = cfg.validate()
    if problems:
        # Degrade visibly: print the problems and exit 0 so cron/systemd don't error-spam while
        # a key is still pending (the watchdog's "not configured -> exit 0" behavior).
        for p in problems:
            logging.getLogger("quest-ai-runner").info("config incomplete: %s", p)
        return 0

    poller = Poller(cfg, state_path=os.getenv("QAR_STATE_PATH", "qar_state.json"))

    if args.check:
        try:
            who = poller.client.whoami()
            logging.getLogger("quest-ai-runner").info("key OK: %s", who)
            return 0
        except Exception as e:  # noqa: BLE001
            logging.getLogger("quest-ai-runner").error("key check failed: %s", e)
            return 1

    if args.once:
        handled = poller.run_once()
        logging.getLogger("quest-ai-runner").info("handled %d task(s)", len(handled))
        return 0

    poller.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
