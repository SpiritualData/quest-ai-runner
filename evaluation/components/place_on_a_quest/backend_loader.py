"""Loads the Quest backend's quest-picking code BY FILE PATH, so the arms measure the shipped functions and no application starts.

Importing ``app.business.quests`` connects to Mongo and Qdrant at import time, which a metric must never do. The two modules loaded here
(``quest_pick_prompt.py``, which imports nothing of the application but its prompt texts, and ``quest_candidates.py``, whose only application imports are
``app.core.cache`` at the top and the model call and prompt inside functions) are read from the sibling ``quest-backend`` checkout
(``QAR_QUEST_BACKEND`` names another) after four small stand-ins for the application's modules are put in ``sys.modules``: a cache decorator that
does not cache, a model call that raises (the arms replace the model step), and the prompt module loaded from its own file. The index
and the semantic search, which read the database, are replaced per case by ``install``."""
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("QAR_QUEST_BACKEND") or Path(__file__).resolve().parents[3].parent / "quest-backend")
loaded_modules = {}


def read_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or not (ROOT / relative).is_file():
        raise RuntimeError(f"quest-backend file {relative} not found under {ROOT}; set QAR_QUEST_BACKEND to a quest-backend checkout")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def stand_in(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def no_cache(**options):  # noqa: ARG001
    return lambda function: function


async def no_model(**options):  # noqa: ARG001
    raise RuntimeError("this stage calls no model")


def backend():
    """``(quest_pick_prompt, quest_candidates)`` as the backend ships them."""
    if not loaded_modules:
        stand_in("app"), stand_in("app.core"), stand_in("app.prompts")
        stand_in("app.core.cache", async_cache=no_cache)
        stand_in("app.core.llm_config", call_llm_async=no_model)
        stand_in("app.prompts.quest_candidate_prompts", **vars(read_module("qb_quest_candidate_prompts", "app/prompts/quest_candidate_prompts.py")))
        loaded_modules["prompts"] = read_module("qb_quest_pick_prompts", "app/prompts/quest_pick_prompts.py")
        stand_in("app.prompts.quest_pick_prompts", **vars(loaded_modules["prompts"]))
        loaded_modules["pick"] = read_module("qb_quest_pick_prompt", "app/business/quests/quest_pick_prompt.py")
        loaded_modules["candidates"] = read_module("qb_quest_candidates", "app/business/quests/quest_candidates.py")
    return loaded_modules["pick"], loaded_modules["candidates"]


def ranking_system_prompt():
    """The system prompt the backend's quest picker sends, from its prompts module."""
    backend()
    return loaded_modules["prompts"].QUEST_RANKING_SYSTEM_PROMPT


def quest_index(candidates, quests):
    """The backend's ``QuestIndex`` for each fixture quest, filled with the labels ``build_user_quest_index`` uses."""
    index = []
    for q in quests:
        entry = candidates.QuestIndex(q["quest_id"], q["outcome"], q["scope"], "", "; ".join(q["open"]))
        entry.add("outcome", q["outcome"])
        entry.add("scope context", q["scope"])
        entry.add("current state", q["state"])
        entry.add("category", q["category"])
        for text in q["open"]:
            entry.add("measurable outcome", text)
        index.append(entry)
    return index
