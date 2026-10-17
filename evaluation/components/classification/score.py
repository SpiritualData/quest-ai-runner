"""Deterministic scoring of one classification case: route must be in ``expect`` (a list), and for a
tool route the tool must be in ``tools`` when the case names them. No model judge: zero extra calls."""
from classification.interface import ROUTES

CONTEXT = {"quest": {"title": "Run a 10K under 55 minutes", "outcome": "Run a 10K in under 55 minutes by December"},
           "tools": ["create_quest_goal", "update_quest_current_state", "add_quest_measurable_outcome",
                     "create_quest_habit", "find_quest_experiments", "create_quest_experiment",
                     "update_quest_experiment", "delete_quest_experiment"]}


def score_case(fn, case):
    out = fn(case["message"], CONTEXT)
    route, tool = out.get("route"), out.get("tool")
    problems = []
    if route not in ROUTES:
        problems.append(f"invalid route {route!r}")
    elif route not in case["expect"]:
        problems.append(f"expected {'/'.join(case['expect'])}")
    if route == "tool" and case.get("tools") and tool not in case["tools"]:
        problems.append(f"tool {tool!r} not in {case['tools']}")
    return dict(ok=not problems, got=f"{route}{':' + tool if tool else ''}",
                note="; ".join(problems) or case["message"][:60], raw=out.get("raw"))
