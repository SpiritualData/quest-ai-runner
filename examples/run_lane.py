#!/usr/bin/env python3
"""Run one executor lane built from ``custom_consumer.build_config`` — discover, claim, run, report.

NOTE: this file predates ``quest_ai_runner.runner.lane.run_lane`` (the library's own shared
``--check``/``--once``/loop-forever driver) and duplicates what that function now does. It is kept
for reference alongside ``custom_consumer.py``'s from-scratch adapter wiring, but a NEW lane should
start from ``examples/minimal_lane.py`` (see ``docs/tutorial-your-first-lane.md``) instead of
copying the loop below.

This mirrors the ``quest-ai-runner`` console entry point but uses an explicitly-built
``RunnerConfig`` from ``examples/custom_consumer.py``, so you can see the whole wiring in one
place and customize it. It degrades visibly (never error-spams) while a key is still pending.

Run modes:
  python examples/run_lane.py --check   # validate the key + identity, then exit
  python examples/run_lane.py --once    # one scan then exit (good for cron)
  python examples/run_lane.py           # loop forever (good for a systemd service)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running as a script (python examples/run_lane.py) from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.custom_consumer import build_config  # noqa: E402
from quest_ai_runner.runner.poller import Poller  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="run_lane",
        description="Run a quest-ai-runner executor lane built from custom_consumer.build_config.",
    )
    p.add_argument("--once", action="store_true", help="one scan then exit (cron)")
    p.add_argument("--check", action="store_true", help="validate config + key, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("run-lane")

    cfg = build_config()
    problems = cfg.validate()
    if problems:
        for prob in problems:
            log.info("lane not fully configured: %s", prob)
        # Don't crash a cron/service over a not-yet-provisioned key or url.
        if any("key" in x or "url" in x for x in problems):
            return 0

    poller = Poller(cfg, state_path=os.getenv("QAR_STATE_PATH", "qar_state.json"))

    if args.check:
        try:
            log.info("key OK -> %s", poller.client.whoami())
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("key check failed: %s", e)
            return 1

    if args.once:
        handled = poller.run_once()
        log.info("handled %d task(s): %s", len(handled), handled)
        return 0

    poller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
