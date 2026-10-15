"""Thin wrapper kept for the old command. The 39 hand-labelled watchdog logs now live in components/deep_review/cases.json; the production prompt, parser and one-shot claude call are impls/live_reviewer.py.

    .venv/bin/python3 evaluation/deep_review_eval.py [run.py options, e.g. --only IDS]

is the same as `components/run.py deep_review --impl live_reviewer`. Needs the claude CLI and makes 39 small calls. Add --record to append the run to results.csv.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    return subprocess.run([sys.executable, str(HERE / "components" / "run.py"), "deep_review", "--impl", "live_reviewer", *sys.argv[1:]]).returncode


if __name__ == "__main__":
    sys.exit(main())
