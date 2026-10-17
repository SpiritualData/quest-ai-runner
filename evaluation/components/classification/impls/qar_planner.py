"""CURRENT solution: QAR's own planner decides the route (Orchestrator._plan, one planning call).
Only the planning step runs: no answer, no tool, no deep run is ever executed. Its tool calls (name and
arguments), confirm and clarify decisions are returned as the router's whole answer."""
import os

import common


def decision_to_route(plan):
    """A PlanDecision as the classification answer (shared with the pre-route arm)."""
    calls = [{"name": c["name"], "args": c.get("args") or {}} for c in (plan.tool_calls or [])]
    if plan.action == "tool" and calls:
        return {"route": "tool", "tool": calls[0]["name"], "calls": calls, "raw": plan.action}
    if plan.action in ("deep", "confirm", "clarify"):
        return {"route": plan.action, "tool": None, "calls": [], "raw": plan.action}
    return {"route": "answer", "tool": None, "calls": [], "raw": plan.action}


def build(meter):
    orch = common.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def classify(message, context):
        quest = context["quest"]
        view = f"Quest: {quest['title']}\nOutcome: {quest['outcome']}"
        return decision_to_route(orch._plan(message, "", view, []))
    return classify
