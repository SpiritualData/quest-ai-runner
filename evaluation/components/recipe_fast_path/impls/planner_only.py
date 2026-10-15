"""BASELINE: QAR's normal path with no recipe store (plan, search context, tool call, reply). The arm every
recipe solution must match on correctness and beat on cost. One orchestrator for the whole run."""
import os

import common


def build(meter):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")
    state = {}

    def handle(message, world):
        if "orch" not in state:
            state["orch"] = common.build_offline_orchestrator(meter, registry=world.registry, planner_tier=tier)
        res = state["orch"].run(message, quest_id="q_eval")
        return {"reply": getattr(res, "text", "") or "", "exit_reason": getattr(res, "exit_reason", None)}
    return handle
