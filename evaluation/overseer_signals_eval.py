"""Thin wrapper kept for the old command. The 16 hand-labelled overseer scenarios, the contrast pairs and the no-exemplar-echo rule now live in components/overseer_signals/ (cases.json, interface.py).

    .venv/bin/python3 evaluation/overseer_signals_eval.py [run.py options, e.g. --only IDS]

is the same as `components/run.py overseer_signals --impl live_model`. Makes ~16 small model calls at QAR_EVAL_MODEL, so it spends tokens. Add --record to append the run to results.csv.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    return subprocess.run([sys.executable, str(HERE / "components" / "run.py"), "overseer_signals", "--impl", "live_model", *sys.argv[1:]]).returncode


if __name__ == "__main__":
    sys.exit(main())
