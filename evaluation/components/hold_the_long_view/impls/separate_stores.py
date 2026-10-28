"""CURRENT: today's carry. The host hands the run its earlier proposals and outcomes (``prior_escalations``); the real
Orchestrator renders the declined ones into the planner's context (``declined_proposals_block``) for this conversation.
Nothing is maintained across conversations or quests. Zero model calls (a scripted planner records its prompt)."""
import common  # noqa: F401
import scripted_brain
from scripted_brain import ScriptedProvider


def build(meter):  # noqa: ARG001
    def carry(case):
        provider = ScriptedProvider([{"action": "answer", "model_tier": "haiku", "rationale": "scripted"}])
        brain = scripted_brain.build_brain(provider)
        brain.cfg.overseer = False
        brain.run(case["message"], prior_escalations=[dict(e) for e in case.get("escalations", [])])
        return {"prompt": provider.plan_prompts[0] if provider.plan_prompts else ""}
    return carry
