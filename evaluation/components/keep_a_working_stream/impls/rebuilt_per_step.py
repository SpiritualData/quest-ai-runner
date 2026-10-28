"""CURRENT: today's design. The real Orchestrator, whose planner is rebuilt every step and, from the second step, swaps
the transcript and the context block for a note saying they were given at step one
(``planner_abbreviate_repeat_context`` on, the shipped default). Model calls are real (the tier of ``QAR_EVAL_MODEL``,
default haiku) over the in-memory quest: no Quest server, no deep runner."""
import os

import common
from quest_ops.fake_quest import WORLD
from quest_ops.impls.qar_orchestrator import record_view

ABBREVIATE = True


def build(meter, abbreviate=None):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")
    flag = ABBREVIATE if abbreviate is None else abbreviate

    def run_request(case, world):
        orch = common.build_offline_orchestrator(meter, registry=WORLD.registry, planner_tier=tier,
                                                 selections={"operate-a-quest": "orchestrator"})
        orch.cfg.planner_abbreviate_repeat_context = flag
        seen = {"steps": 0}
        original = orch._plan

        def counted(*args, **kwargs):
            seen["steps"] += 1
            return original(*args, **kwargs)
        orch._plan = counted
        view = "\n\n".join(p for p in (case.get("context_view", ""), record_view(world)) if p)
        before_in, before_out = meter.tokens_in, meter.tokens_out
        res = orch.run(case["message"], quest_id=world.quest_id, context_view=view,
                       transcript=case.get("transcript", ""), prior_escalations=case.get("prior_escalations"))
        return {"reply": getattr(res, "text", "") or "", "steps": seen["steps"],
                "tokens_in": meter.tokens_in - before_in, "tokens_out": meter.tokens_out - before_out}
    return run_request
