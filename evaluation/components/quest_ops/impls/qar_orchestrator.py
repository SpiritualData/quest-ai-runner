"""CURRENT solution: QAR's whole brain handles the turn (plan, tool call through the registry, reply).
Deep runner disabled, so a request it would send to a deep run just comes back unexecuted.

A FRESH orchestrator (and scratch corpus) per case, so no case can see another's turns: a shared one
answered "already on your record" to a request whose earlier twin ran in a different case. The quest's
current record is handed in as ``context_view``, the slot production fills from Quest."""
import os

import common
from quest_ops.fake_quest import WORLD


def record_view(world):
    s = world.state
    lines = ["Quest record (current):", f"Current state: {s['current_state']}"]
    lines += [f"Goal: {g.get('title')} ({g.get('period')})" for g in s["goals"]]
    lines += [f"Habit: {h.get('name')}" for h in s["habits"]]
    lines += [f"Measurable outcome: {o.get('text')}" for o in s["outcomes"]]
    lines += [f"Experiment {e['id']}: {e['question']} [{e['status']}]" for e in s["experiments"]]
    return "\n".join(lines)


def build(meter):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")

    def run_operation(message, world):
        orch = common.build_offline_orchestrator(meter, registry=WORLD.registry, planner_tier=tier)
        res = orch.run(message, quest_id=world.quest_id, context_view=record_view(world))
        return {"reply": getattr(res, "text", "") or ""}
    return run_operation
