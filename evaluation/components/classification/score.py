"""Deterministic scoring of one classification case, over the router's whole job. No model judge: zero extra calls.

A case passes only when every check it carries holds:
  expect       the route is one of these
  tools        on a tool route at least one call is one of these (a find before a create is a fine first move)
  arg_checks   [{"tool", "contains": {arg: substring}}]: when that tool is called, the stated detail survives in
               its arguments (the named argument, or any other argument of that call: a goal's number may land
               in its title or its criteria)
  must_call    every listed tool appears among the calls (multi-step requests)
  forbid_args  {arg: value}: no call carries that value (a first destructive ask never confirms itself)
A tool route for a message that expects only answer/confirm/clarify fails by its route (an unrequested write)."""
from classification.interface import ROUTES

CONTEXT = {"quest": {"title": "Run a 10K under 55 minutes", "outcome": "Run a 10K in under 55 minutes by December"},
           "tools": ["create_quest_goal", "update_quest_current_state", "add_quest_measurable_outcome",
                     "create_quest_habit", "find_quest_experiments", "create_quest_experiment",
                     "update_quest_experiment", "delete_quest_experiment"],
           "direct_tools": ["create_quest_goal", "update_quest_current_state", "add_quest_measurable_outcome",
                            "create_quest_habit", "find_quest_experiments", "create_quest_experiment",
                            "update_quest_experiment"]}


def score_case(fn, case):
    out = fn(case["message"], CONTEXT)
    route, tool = out.get("route"), out.get("tool")
    calls = out.get("calls") or ([{"name": tool, "args": out.get("args") or {}}] if tool else [])
    problems = []
    if route not in ROUTES:
        problems.append(f"invalid route {route!r}")
    elif route not in case["expect"]:
        problems.append(f"expected {'/'.join(case['expect'])}")
    if route == "tool" and case.get("tools") and not any(c["name"] in case["tools"] for c in calls):
        problems.append(f"no call to {case['tools']} (got {[c['name'] for c in calls]})")
    if route == "tool":
        for chk in case.get("arg_checks", []):
            for call in (c for c in calls if c["name"] == chk["tool"]):
                for arg, sub in chk["contains"].items():
                    everything = " ".join(str(v) for v in call["args"].values()).lower()
                    if sub.lower() not in str(call["args"].get(arg, "")).lower() and sub.lower() not in everything:
                        problems.append(f"{chk['tool']}.{arg} lacks {sub!r}")
        for name in case.get("must_call", []):
            if name not in [c["name"] for c in calls]:
                problems.append(f"missing call {name}")
    for call in calls:
        for arg, value in case.get("forbid_args", {}).items():
            if call["args"].get(arg) == value:
                problems.append(f"{call['name']} sent {arg}={value} on the first ask")
    return dict(ok=not problems, got=f"{route}{':' + ','.join(c['name'] for c in calls) if calls else ''}",
                note="; ".join(problems) or case["message"][:60], raw=out.get("raw"))
