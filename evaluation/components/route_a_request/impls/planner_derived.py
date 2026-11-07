"""CURRENT: QAR's planner decides, and the route is read off its decision (``PlannerDerived``). Only the planning step runs: no answer, no tool, no deep run is ever executed.
Paid: one planning call per request; stage it with ``--sample N`` and ``--max-calls``."""
import os

import offline_orchestrator
from quest_ai_runner.the_intelligence.mind.request_router.planner_derived import report_of
from hubofhubs.api import route_from_report


def build(meter):
    orch = offline_orchestrator.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def route(kind, args):  # noqa: ARG001
        view = str((args.get("context") or {}).get("view") or "")
        return route_from_report(report_of(orch._plan(args["request"], "", view, [])))["route"]
    return route
