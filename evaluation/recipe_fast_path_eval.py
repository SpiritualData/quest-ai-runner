"""Thin wrapper kept for the old command. The measurement now lives behind the component interface in
``components/recipe_fast_path/`` (cases in its ``cases.json``, fake tool world in ``fake_world.py``, the two
arms as ``impls/planner_only.py`` and ``impls/recipe_replay.py``). Uses a REAL model through the harness
(``QAR_EVAL_MODEL``, default haiku), so it spends tokens.

    python evaluation/recipe_fast_path_eval.py --subset 1|10|30 [--arm baseline|recipes|both]

is the same as ``components/run.py recipe_fast_path --impl planner_only|recipe_replay [--only IDS]``.
Differences from the old script: the context corpus is the harness's empty scratch corpus (the old one
copied eight docs), and per-ask wall time / tokens are in the harness rows rather than a median summary.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE / "components" / "recipe_fast_path" / "cases.json").read_text())
SUBSET_10 = ["RH1", "RH2", "RN1", "RN2", "RT1", "RS1", "RG1", "NM1", "NM3", "U1"]
SUBSETS = {1: ["RH1"], 10: SUBSET_10, 30: [c["id"] for c in CASES]}
ARMS = {"baseline": "planner_only", "recipes": "recipe_replay"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", type=int, choices=sorted(SUBSETS), required=True)
    ap.add_argument("--arm", choices=["baseline", "recipes", "both"], default="both")
    a = ap.parse_args()
    for arm in (ARMS if a.arm == "both" else [a.arm]):
        subprocess.run([sys.executable, str(HERE / "components" / "run.py"), "recipe_fast_path", "--impl",
                        ARMS[arm], "--only", ",".join(SUBSETS[a.subset])], check=False)


if __name__ == "__main__":
    main()
