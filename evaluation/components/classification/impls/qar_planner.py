"""CURRENT solution: QAR's own planner decides the route (Orchestrator._plan, one planning call).
Only the planning step runs: no answer, no tool, no deep run is ever executed."""
import os

import common


def build(meter):
    orch = common.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def classify(message, context):
        quest = context["quest"]
        view = f"Quest: {quest['title']}\nOutcome: {quest['outcome']}"
        plan = orch._plan(message, "", view, [])
        calls = [c["name"] for c in (plan.tool_calls or [])]
        if plan.action == "tool" and calls:
            return {"route": "tool", "tool": calls[0], "raw": plan.action}
        if plan.action == "deep":
            return {"route": "deep", "tool": None, "raw": plan.action}
        return {"route": "answer", "tool": None, "raw": plan.action}
    return classify
