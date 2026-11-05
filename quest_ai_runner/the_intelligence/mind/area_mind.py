"""``the-mind``: every step of a request acting on what the request has established so far, at the cost of the fast tier.

QAR's area arm ``rebuilt_per_step`` is the Mind for one request, run by QAR's own loop (plan, gather, re-plan, answer or hand deep work on),
and offers the hub's one line, ``think(request, ...)``: the mapping with ``text``, ``results`` (one per piece handed to a deep worker),
``pieces`` and ``steps`` the hub's Mind returns. QAR's loop reads the representative's authority, the guidance and the quest's cards from
its own configuration, so ``think`` takes the hub's other inputs only to keep the hub's signature; the model asked at a tier is Model
Plumbing's ``ask`` (the hub's ``via_vendor`` arm over QAR's vendor caller and tier chooser), no longer a line of the Mind.
The members are the parts of one step of the loop: the prompt it assembles (``assemble-a-prompt``), the turn it answers
(``answer-a-turn``, ``answer-end-to-end``), the operations it replays (``replay-known-operations``), the classification of its message
(``core-ml-tasks``), the interim message it may send (``reply-in-steps``) and the trail it shows (``show-the-ais-work``). Members the line
does not need are built on first use.
"""
from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional, Sequence

from esoi import implementation
from esoi import Lazy
from hubofhubs.api import ModelCalling
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.interface import RoutedModelCalling

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import HUB_TIER_OF
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.interface import RegistryTierSelection
from quest_ai_runner.the_intelligence.mind.planner.recipes import Recipe, RecipeStore
from quest_ai_runner.the_intelligence.mind.response_writer.work_trace import WorkShowing
from quest_ai_runner.the_self.loop_wiring import orchestrator_over
from quest_ai_runner.the_self.interface import GoalAnswer
from quest_ai_runner.the_intelligence.mind.planner.message_routing import route_message
from quest_ai_runner.the_intelligence.mind.planner.interface import PromptAssembling


class PromptCapture:
    """Stands in for the model while the planner prompt is written: keeps the prompt it was handed and decides nothing."""

    def __init__(self) -> None:
        self.prompt = ""

    def plan(self, prompt: str, *, model: str, tool_schema: Any, layers: Any = None) -> dict:
        self.prompt = prompt
        return {}

    def answer(self, messages: Any, *, model: str, system: Any = None, layers: Any = None) -> str:
        return ""

    def list_models(self) -> List[str]:
        return []



@implementation("context-intake", "planner_prompt")
class PlannerPrompt:
    """The planner's prompt for a message, written by the real loop with a model that only records what it is asked. No model call."""
    REQUIRES = ()     # the loop is built over a recorder, not a model

    def assemble(self, message: str) -> str:
        capture = PromptCapture()
        orchestrator_over(capture, deep_runner=None)._plan(message, "", "", [])
        return capture.prompt


def turn_of(model: Any, message: str) -> GoalAnswer:
    """One turn of the orchestrator over ``model`` (no cards behind it), as the answer a caller reads."""
    result = orchestrator_over(model).run(message)
    return GoalAnswer(text=result.text or result.question or "", kind=result.kind, exit_reason=result.exit_reason,
                      ran=tuple(result.goals), question=result.question or "", steps=result.steps)



@implementation("quest-self", "orchestrator_turn")
class OrchestratorTurn:
    """One turn of the orchestrator over the model, with no cards behind it."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def turn(self, message: str) -> GoalAnswer:
        return turn_of(self.model, message)



@implementation("quest-self", "shipped_path")
class ShippedPath:
    """The shipped path is one turn of the loop over the model; this arm returns that turn whole."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def answer(self, message: str) -> GoalAnswer:
        return turn_of(self.model, message)



@implementation("planner", "stored_recipes")
class StoredRecipes:
    """The recipe store of ``core/recipes.py``: one JSON file when a path is given, otherwise held in memory."""
    REQUIRES = ()     # a file path or nothing

    def __init__(self, path: Optional[str] = None) -> None:
        self.store = RecipeStore(path)

    def match(self, request: str) -> Sequence[Any]:
        return self.store.match(request)

    def learn(self, request: str, tool: str, args: Mapping[str, Any]) -> Optional[Recipe]:
        return self.store.learn(request, tool, dict(args))



@implementation("planner", "router_labels")
class RouterLabels:
    """The classification is the route the loop's planner decides: ``answer``, ``tool``, ``deep``, ``confirm`` or ``clarify``."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def classify(self, message: str) -> str:
        return str(route_message(self.model, message)["route"])



@implementation("the-mind", "rebuilt_per_step")
class RebuiltPerStep:
    """Each step is a fresh planner call built from the template, the cached head and context layers and the newest observations.
    ``think`` runs one turn of that loop (its model is a need of the arm, so a missing model program is a pending need, not an error in the middle of a turn) and returns the hub's Mind mapping; the other methods hand a message to the member that owns that
    part of a step."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, provider: RoutedModelCalling, prompts: Lazy[PromptAssembling], tiers: Lazy[RegistryTierSelection],
                 work: Lazy[WorkShowing]) -> None:
        self.provider, self.prompts, self.tiers, self.work = provider, prompts, tiers, work

    def think(self, request: str, authority: Optional[Mapping[str, Any]] = None, target: Optional[Mapping[str, Any]] = None,
              guidance: Optional[Mapping[str, Any]] = None, profile: Optional[Any] = None, prepared: Optional[Mapping[str, Any]] = None,
              unmet: Optional[Mapping[str, Any]] = None, outside: Optional[Mapping[str, Any]] = None, budget: Optional[Any] = None,
              cancelled: Optional[Callable[[], bool]] = None) -> Mapping[str, Any]:
        answer = turn_of(self.provider, request)
        return {"text": answer.text, "results": [{"piece": goal, "worker": "delegated-agent-sessions"} for goal in answer.ran],
                "pieces": list(answer.ran), "steps": answer.steps, "kind": answer.kind, "exit_reason": answer.exit_reason,
                "question": answer.question}

    def starts_on(self, tier: str) -> str:
        """The model a step of the loop starts on for a tier: the one the tier chooser measured best for it (a tier it does not list comes back as given)."""
        arm = (self.tiers.choose().get(HUB_TIER_OF.get(tier, tier)) or {}).get("arm")
        return arm[1] if arm else tier

    def step_prompt(self, message: str) -> str:
        return self.prompts.assemble(message)

    def trail(self, emit: Any, context_meta: Optional[Mapping[str, Any]]) -> Any:
        return self.work.start(emit, context_meta)
