"""Run the second real tier (``real_cases2.json``, frozen before its first run) for one arm and record it with that file's own hash.

    python3 real2_run.py --impl gated [--record] [--note TEXT] [--only ID,ID]

The turns run only on the Claude Code subscription (``QAR_MODEL_BACKEND=claude_cli``); the case file is read through ``score.load_cases``."""
import hashlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["QAR_EVAL_LIVE_TURNS"] = "1"
os.environ["QAR_EVAL_REAL_CASES"] = "real_cases2.json"
os.environ["QAR_INTERIM_TURNS_FILE"] = "real_cases2.json"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2]))


def main():
    import common
    from esoi import harness
    digest = hashlib.sha256((HERE / "real_cases2.json").read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:8]
    harness.cases_digest = lambda component, eval_dir: digest
    harness.record_slices = lambda *a, **k: []   # the per-case log compacts rows pinned to cases.json's hash; this tier's rows go to results.csv only
    harness.run_component("interim_reply", __import__("interim_reply.score", fromlist=["x"]).score_case, sys.argv[1:], common.CONFIG)


if __name__ == "__main__":
    main()
