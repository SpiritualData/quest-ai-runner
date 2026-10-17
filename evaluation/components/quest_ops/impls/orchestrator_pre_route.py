"""The LIVE default: the same whole brain as ``qar_orchestrator`` with the ``route-a-message`` selection
``pre_route``, so the cheap pre-route may settle a clear request for a direct tool before the planner runs.
Everything else (answers, reads, confirms, deep) is the planner's, as in ``qar_orchestrator``."""
import os

import common
from quest_ops.impls.qar_orchestrator import record_view
from quest_ops.fake_quest import WORLD


def build(meter):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")

    def run_operation(message, world):
        orch = common.build_offline_orchestrator(meter, registry=WORLD.registry, planner_tier=tier,
                                                 selections={"route-a-message": "pre_route"})
        res = orch.run(message, quest_id=world.quest_id, context_view=record_view(world))
        return {"reply": getattr(res, "text", "") or ""}
    return run_operation
