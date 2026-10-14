"""One command per component:

    PY=.venv/bin/python3        # from the quest-ai-runner repo root
    $PY evaluation/components/run.py classification --impl qar_planner
    $PY evaluation/components/run.py quest_ops --impl qar_orchestrator

See README.md. The two components share only common.py (call meter, offline orchestrator)."""
import sys

import common

COMPONENTS = ("classification", "quest_ops")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMPONENTS:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    scorer = __import__(f"{name}.score", fromlist=["score_case"]).score_case
    common.run_component(name, scorer, sys.argv[2:])
