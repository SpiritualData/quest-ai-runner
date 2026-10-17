"""BASELINE, offline arm: the same live Orchestrator, fake tool world and rule model with no recipe store, so
every ask takes the normal plan, tool and answer path. Parity with ``recipe_replay_offline`` on correctness
and a higher model-call count is the claim being checked."""
import scripted_brain
from recipe_fast_path.rule_model import RuleModel


def build(meter):
    model = RuleModel(meter)

    def handle(message, world):
        model.current, model.acted = message, set()
        orch = scripted_brain.build_brain(model, tools=world.registry)
        res = orch.run(message, quest_id="q_eval")
        return {"reply": getattr(res, "text", "") or "", "exit_reason": getattr(res, "exit_reason", None)}
    return handle
