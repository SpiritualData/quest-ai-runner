"""One command per component:

    PY=.venv/bin/python3        # from the quest-ai-runner repo root
    $PY evaluation/components/run.py classification --impl qar_planner
    $PY evaluation/components/run.py quest_ops --impl qar_orchestrator
    $PY evaluation/components/run.py context_selection --impl idf_docstrings
    $PY evaluation/components/run.py recipe_fast_path --impl recipe_replay

Add `--record` to append the run to results.csv; `leaderboard.py` compares implementations.
See README.md. The components share only common.py (call meter, offline orchestrator)."""
import sys

import common

COMPONENTS = common.discover_components()  # every folder with a score.py

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMPONENTS:
        raise SystemExit(__doc__ + "\nComponents: " + ", ".join(COMPONENTS)
                         + "\nCompare implementations: leaderboard.py")
    name = sys.argv[1]
    score = __import__(f"{name}.score", fromlist=["score_case"])
    _, rows = common.run_component(name, score.score_case, sys.argv[2:])
    if hasattr(score, "aggregate"):  # a component with its own headline metric (e.g. top-1 / top-3)
        print(f"{name}: {score.aggregate(rows)}")
