"""CURRENT: reads the evaluation folders and the results log. A task is measured when the component the case names has a case file with
labelled expectations and its latest row for the named arm passes at or above the combined metric's bar. A task that names no
component is not measured. Reads only; zero model calls."""
import json

import common
from esoi import leaderboard
from qar_overall import combine


def case_count(component):
    path = common.HERE / component / "cases.json"
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0


def build(meter):  # noqa: ARG001
    history = leaderboard.all_results(common.CONFIG.results_path)
    bar = combine.settings()["bar"]

    def measured(case):
        component, arm = case.get("component"), case.get("arm")
        if not component:
            return {"measured": False, "rate": None, "why": "no component measures this task"}
        if not case_count(component):
            return {"measured": False, "rate": None, "why": f"{component} has no labelled cases"}
        rows = history.get(component, {}).get(arm)
        if not rows:
            return {"measured": False, "rate": None, "why": f"{component}/{arm} has no recorded row"}
        rate = leaderboard.pass_rate(rows[-1])
        return {"measured": rate >= bar, "rate": round(rate, 4), "why": f"{component}/{arm} latest row reads {rate:.3f}, bar {bar}"}
    return measured
