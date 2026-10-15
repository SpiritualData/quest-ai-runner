"""CURRENT: QAR with a ``RecipeStore``. The first time an operation succeeds the store learns a recipe; a
later request that matches it replays the tool call before any context search. Same orchestrator,
registry and model as ``planner_only``; only the recipe store differs, so the comparison is the fast path."""
import os
import tempfile
from pathlib import Path

import common
from quest_ai_runner.core.recipes import RecipeStore


def build(meter):
    tier = os.environ.get("QAR_EVAL_MODEL", "haiku")
    state = {}

    def handle(message, world):
        if "orch" not in state:
            store = RecipeStore(Path(tempfile.mkdtemp(prefix="qar_recipes_")) / "recipes.json")
            state["orch"] = common.build_offline_orchestrator(meter, registry=world.registry, planner_tier=tier,
                                                              recipe_store=store)
        res = state["orch"].run(message, quest_id="q_eval")
        return {"reply": getattr(res, "text", "") or "", "exit_reason": getattr(res, "exit_reason", None)}
    return handle
