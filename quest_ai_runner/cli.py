"""Console entry point for the poller (``quest-ai-runner``).

This is the thin CLI that runs the EXECUTOR lane. It builds a RunnerConfig from environment
variables (so a stranger's org can run it with no code) and starts the Poller in ``--once``
(cron) or loop (service) mode. NO consumer-specific defaults: every value comes from env.

Env it reads:
  QUEST_BASE_URL, QUEST_API_KEY, QUEST_TEAM_ID   — the Quest connection (key is qsk_...)
  QAR_CORPUS_ROOT                                — file root for the FilesAdapter (grounding)
  QAR_DEEP_WORKING_DIR                           — working dir for the subprocess deep-runner
  QAR_CLAUDE_PATH (optional)                     — the worker binary (default: claude on PATH)
  QAR_STATE_PATH (optional)                      — signature store path (default: ./qar_state.json)
  QAR_POLL_INTERVAL (optional, seconds)          — loop cadence (default 900)
  ANTHROPIC_API_KEY                              — for the model provider (planner/answer/models)

A consumer that wants finer control imports the library and builds RunnerConfig itself instead.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .adapters import AnthropicProvider, FilesAdapter
from .config import RunnerConfig
from .core.goal_runner import SubprocessConfig, SubprocessGoalRunner
from .runner.poller import Poller


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
        model_provider=AnthropicProvider(),
        deep_runner=deep_runner,
        corpus_root=corpus,
    )
    if os.getenv("QAR_POLL_INTERVAL"):
        cfg.poll_interval_seconds = float(os.environ["QAR_POLL_INTERVAL"])
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
