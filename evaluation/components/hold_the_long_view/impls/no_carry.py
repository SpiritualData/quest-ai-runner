"""BASELINE: the floor. The run is given no earlier proposals at all, so nothing earlier can reach the planner."""
import common  # noqa: F401
import scripted_brain
from scripted_brain import ScriptedProvider


def build(meter):  # noqa: ARG001
    def carry(case):
        provider = ScriptedProvider([{"action": "answer", "model_tier": "haiku", "rationale": "scripted"}])
        brain = scripted_brain.build_brain(provider)
        brain.cfg.overseer = False
        brain.run(case["message"])
        return {"prompt": provider.plan_prompts[0] if provider.plan_prompts else ""}
    return carry
