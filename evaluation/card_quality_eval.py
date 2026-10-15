"""Thin wrapper kept for the old command. The learn-then-select round trip (three learned tasks, five queries) now lives in components/card_quality/ (world.json, cases.json; impls/live_updater_hybrid.py is the original flow).

    .venv/bin/python3 evaluation/card_quality_eval.py [run.py options, e.g. --only IDS]

is the same as `components/run.py card_quality --impl live_updater_hybrid`. Needs a model backend, a Voyage key and qdrant_client; spends tokens. The offline arm is --impl seeded_keyword_store. Add --record to append the run to results.csv.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    return subprocess.run([sys.executable, str(HERE / "components" / "run.py"), "card_quality", "--impl", "live_updater_hybrid", *sys.argv[1:]]).returncode


if __name__ == "__main__":
    sys.exit(main())
