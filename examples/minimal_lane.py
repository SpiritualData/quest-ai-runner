#!/usr/bin/env python3
"""A minimal quest-ai-runner lane — see docs/tutorial-your-first-lane.md for the walkthrough.

This is deliberately small. The point is the CONTRAST with a hand-rolled consumer: three separate
consumers of this library each wrote their own ~40-line ``--check``/``--once``/loop-forever driver
around ``Poller`` (two of them then extracted a shared copy — but OUTSIDE the library, so the third
had nowhere to find it), and the biggest of the three also wrote ~250 lines of persona-resolution
machinery from scratch. None of that is a lane's job any more: the driver is
``runner.lane.run_lane``, and persona resolution is ``RunnerConfig.personas`` (see
``docs/personas.md``). What is left, below, is what is ACTUALLY specific to this lane — which for
the minimal case is nothing beyond "read config from a file plus the environment".

Config comes from ``qar.toml`` (in this directory) layered under whatever ``QUEST_*``/``QAR_*``
environment variables are set — an env var always wins on any field both set. NO secrets, real ids,
or absolute paths are baked in here or in ``qar.toml``; the Quest connection is read from the
environment (see ``.env.example`` at the repo root).

Run modes (identical to the installed ``quest-ai-runner`` console entry point):
  python examples/minimal_lane.py --check   # validate the key + identity, then exit
  python examples/minimal_lane.py --once    # one scan then exit (good for cron)
  python examples/minimal_lane.py           # loop forever (good for a systemd service)
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script (python examples/minimal_lane.py) from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quest_ai_runner import load_config  # noqa: E402
from quest_ai_runner.runner.lane import run_lane  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / "qar.toml"


def build_config():
    return load_config(str(CONFIG_PATH))


def main(argv=None) -> int:
    return run_lane(
        argv,
        prog="minimal-lane",
        description="A minimal quest-ai-runner lane (see docs/tutorial-your-first-lane.md).",
        lane_label="minimal-lane",
        log_name="minimal-lane",
        build_config=build_config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
