"""CURRENT: the production ``route_goals`` with a scripted judge and a pinned clock. Zero model calls."""
import json
from datetime import datetime, timezone

from quest_ai_runner.intelligence.proactivity.autopilot_routing import route_goals

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)


def build(meter):  # noqa: ARG001
    def route(args):
        said = args["judge"]

        def judge(prompt):  # noqa: ARG001
            if said == "FAIL":
                raise RuntimeError("the judge is down")
            rows = []
            for goal, autopilot in said.items():
                for one in (autopilot if isinstance(autopilot, list) else [autopilot]):
                    rows.append({"goal": goal, "autopilot": one, "reason": "fits"})
            return json.dumps({"assignments": rows})
        payload = {"period_groups": [{"time_scope": "day", "period": "2026-07-12", "goals": args["goals"]}]}
        found = route_goals(payload, args["duty"], judge, NOW)
        return {"assignments": {row["goal_id"]: row["autopilot_id"] for row in found if row["autopilot_id"]}}
    return route
