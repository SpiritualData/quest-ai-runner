"""Loads the Quest backend's quest-picking code BY FILE PATH, so the arms measure the shipped functions and no application starts.

Importing ``app.business.quests`` connects to Mongo and Qdrant at import time, which a metric must never do. The modules loaded here
(``quest_aim.py``, which imports nothing, ``quest_pick_prompt.py``, which imports nothing of the application but its prompt texts, ``quest_aim`` and
``quest_examples`` (also import free), and ``quest_candidates.py``, whose only application imports are
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
        stand_in("app"), stand_in("app.core"), stand_in("app.prompts"), stand_in("app.business"), stand_in("app.business.quests")
        stand_in("app.core.cache", async_cache=no_cache)
        loaded_modules["aim"] = read_module("qb_quest_aim", "app/business/quests/quest_aim.py")
        stand_in("app.business.quests.quest_aim", **vars(loaded_modules["aim"]))
        loaded_modules["examples"] = read_module("qb_quest_examples", "app/business/quests/quest_examples.py")
        stand_in("app.business.quests.quest_examples", **vars(loaded_modules["examples"]))
        stand_in("app.core.llm_config", call_llm_async=no_model)
        stand_in("app.prompts.quest_candidate_prompts", **vars(read_module("qb_quest_candidate_prompts", "app/prompts/quest_candidate_prompts.py")))
        loaded_modules["prompts"] = read_module("qb_quest_pick_prompts", "app/prompts/quest_pick_prompts.py")
        stand_in("app.prompts.quest_pick_prompts", **vars(loaded_modules["prompts"]))
        loaded_modules["pick"] = read_module("qb_quest_pick_prompt", "app/business/quests/quest_pick_prompt.py")
        loaded_modules["candidates"] = read_module("qb_quest_candidates", "app/business/quests/quest_candidates.py")
    return loaded_modules["pick"], loaded_modules["candidates"]


def examples():
    """The backend's ``quest_examples`` module (the pure part: item shapes, the vote, the fusion), as it ships."""
    backend()
    return loaded_modules["examples"]


def ranking_system_prompt():
    """The system prompt the backend's quest picker sends, from its prompts module."""
    backend()
    return loaded_modules["prompts"].QUEST_RANKING_SYSTEM_PROMPT


def quest_doc(quest):
    """A fixture quest in the backend's stored document shape (``quest_aim`` and ``quest_index_from_doc`` read it): the optional ``vision``
    (the vision statement; the outcome when absent) and ``purpose`` pass through."""
    return {"quest_id": quest["quest_id"], "outcome": quest["outcome"], "vision_statement": quest.get("vision") or quest["outcome"],
            "purpose": quest.get("purpose") or "", "category": quest["category"], "current_state": quest["state"],
            "scope": quest["scope"], "measurable_outcomes": [{"text": text} for text in quest["open"]]}


def quest_aim(quest):
    """``{"vision", "purpose"}`` of a fixture quest, from the backend's own ``quest_aim``: the text every quest picker reads."""
    backend()
    return loaded_modules["aim"].quest_aim(quest_doc(quest))


def quest_index(candidates, quests):
    """The backend's ``QuestIndex`` for each fixture quest, from its own ``quest_index_from_doc`` (the one projection production uses)."""
    return [candidates.quest_index_from_doc(quest_doc(q)) for q in quests]


def verify():
    """The backend's ``quest_verify`` module (the verify step after the judge's pick), as it ships; its prompt texts and the pick prompt module are
    stood in for the application imports it makes."""
    backend()
    if "verify" not in loaded_modules:
        stand_in("app.business.quests.quest_pick_prompt", **vars(loaded_modules["pick"]))
        stand_in("app.prompts.quest_verify_prompts", **vars(read_module("qb_quest_verify_prompts", "app/prompts/quest_verify_prompts.py")))
        loaded_modules["verify"] = read_module("qb_quest_verify", "app/business/quests/quest_verify.py")
    return loaded_modules["verify"]
