"""CURRENT, live: the code that holds a deep goal to its metric (``core.goal_metrics.GoalMetrics``: slice, reading before, the
runner's own record of a change, reading after, reconcile) driven over an in-memory ledger and a meter that returns the case's
true readings. A chat turn and in-process work pass through nothing today, so they come back with no rows, and nothing in the
code proposes a metric or answers a regression. Zero model calls."""
from types import SimpleNamespace

import common  # noqa: F401
from esoi.metric_meter import AFTER, BEFORE
from esoi.spec_slice import SpecSlice
from quest_ai_runner.core.goal_metrics import GoalMetrics


class MemoryLedger:
    def __init__(self):
        self.rows = []

    def record(self, result):
        self.rows.append(result)
        return True

    def for_run(self, run_id):
        return [r for r in self.rows if r.run_id == run_id]


class Meter:
    """A meter that returns one moment's true reading for the case."""
    name = "fixed"
    cheap = True

    def __init__(self, serves, value):
        self.serves, self.value = (serves,), value

    def measure(self, spec_slice, working_dir=None):  # noqa: ARG002
        return self.value


class Iteration:
    """The arm under the goal loop, with meters that read the case's true values."""
    name = "slice-and-writeback"
    measures = True

    def __init__(self, case):
        self.case = case
        self.source = self if case["has_metric"] else None
        self.meters = [Meter(BEFORE, case.get("before")), Meter(AFTER, case.get("after"))]

    def slice_for(self, request, working_dir=None):  # noqa: ARG002
        return SpecSlice(root="", solution_id="s", metric_id="m", metric_name="metric")


def build(meter):  # noqa: ARG001
    def hold(case):
        if case["kind"] != "deep":
            return {"rows": [], "missing": False, "proposed": False, "response": ""}
        gm = GoalMetrics(Iteration(case), MemoryLedger(), "run1", request="r", gate_applies=True)
        gm.begin()
        res = SimpleNamespace(has_write_receipt=bool(case["changed"]), changes_landed=0, output="done", decision_id=None)
        gm.note_attempt(res, [])
        gm.finish(res)
        rows = [{"before": m.get("before"), "after": m.get("after"), "source": m.get("source")} for m in res.metric_results]
        return {"rows": rows, "missing": bool(res.metric_missing), "proposed": False, "response": ""}
    return hold
