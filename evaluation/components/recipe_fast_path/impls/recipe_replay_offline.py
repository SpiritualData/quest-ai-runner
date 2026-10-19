"""CURRENT, offline arm: the live Orchestrator with a live ``RecipeStore`` and the fake tool world, with a
deterministic rule model (``rule_model.py``) in place of the claude CLI. A success teaches a recipe; a later
matching request replays it through the one fill-args call. Measures the replay engine given a model that
judges well (an optimistic ceiling), with the model-call count the fast path actually makes."""

SIMULATED = True   # meters a deterministic rule model: recorded as model 'simulated' at cost 0.0, compared on calls
import tempfile
from pathlib import Path

import scripted_brain
from quest_ai_runner.core.recipes import RecipeStore
from recipe_fast_path.rule_model import RuleModel


def build(meter):
    model = RuleModel(meter)
    store = RecipeStore(Path(tempfile.mkdtemp(prefix="qar_recipes_offline_")) / "recipes.json")

    def handle(message, world):
        model.current, model.acted = message, set()
        orch = scripted_brain.build_brain(model, tools=world.registry, recipes=store)
        res = orch.run(message, quest_id="q_eval")
        return {"reply": getattr(res, "text", "") or "", "exit_reason": getattr(res, "exit_reason", None)}
    return handle
