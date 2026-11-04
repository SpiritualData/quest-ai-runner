"""``reason-and-answer``: every step of a request acting on what the request has established so far, at the cost of the fast tier.

The area arm ``rebuilt_per_step`` takes the area's members as typed seats and offers the area's one line, ``reason(prompt, tier)``:
the prompt asked of the model the tier chooser measured best for the tier (the hub's ``reason-and-answer`` interface). The members
are ``call-a-model`` and ``choose-a-model-tier`` (used by ``reason``), and the parts of one step of the loop: the prompt it assembles
(``assemble-a-prompt``), the turn it answers (``answer-a-turn``, ``answer-end-to-end``), the operations it replays
(``replay-known-operations``), the classification of its message (``core-ml-tasks``), the interim message
it may send (``reply-in-steps``) and the trail it shows (``show-the-ais-work``). Members the line does not need are built on first use.
"""
from __future__ import annotations

import typing
from typing import Any, List, Mapping, Optional, Sequence

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.seats import ModelCalling, Reasoning

from .core.interim_policy import InterimDecision, InterimPolicy, InterimState
from .core.model_registry import HUB_TIER_OF, RegistryTierSelection
from .core.recipes import Recipe, RecipeStore
from .core.work_trace import WorkShowing
from .loop_wiring import GoalAnswer, orchestrator_over
from .message_routing import route_message
from .providers import RoutedModelCalling


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


@solution("assemble-a-prompt", kind="event", entry="assemble", example="What should I work on first this week?")
@typing.runtime_checkable
class PromptAssembling(typing.Protocol):
    """Writes the prompt a step of the loop sends for a message: the planner's view of it, as text."""

    def assemble(self, message: str) -> str: ...


@implementation("assemble-a-prompt", "planner_prompt")
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


@solution("answer-a-turn", kind="event", entry="turn", example="What should I work on first this week?")
@typing.runtime_checkable
class TurnAnswering(typing.Protocol):
    """Answers one chat turn: the loop plans, gathers, re-plans and answers, hands deep work on or asks to confirm."""

    def turn(self, message: str) -> GoalAnswer: ...


@implementation("answer-a-turn", "orchestrator_turn")
class OrchestratorTurn:
    """One turn of the orchestrator over the model, with no cards behind it."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def turn(self, message: str) -> GoalAnswer:
        return turn_of(self.model, message)


@solution("answer-end-to-end", kind="event", entry="answer", example="What should I work on first this week?")
@typing.runtime_checkable
class EndToEnd(typing.Protocol):
    """The path as shipped, from a message to the finished answer in one call: what the end-to-end metric measures."""

    def answer(self, message: str) -> GoalAnswer: ...


@implementation("answer-end-to-end", "shipped_path")
class ShippedPath:
    """The shipped path is one turn of the loop over the model; this arm returns that turn whole."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def answer(self, message: str) -> GoalAnswer:
        return turn_of(self.model, message)


@solution("replay-known-operations", kind="event", entry="match", example="Add a note that the launch moves to Friday")
@typing.runtime_checkable
class OperationReplaying(typing.Protocol):
    """Finds the operations learned from earlier requests that a new request repeats, best first, each with its similarity."""

    def match(self, request: str) -> Sequence[Any]: ...


@implementation("replay-known-operations", "stored_recipes")
class StoredRecipes:
    """The recipe store of ``core/recipes.py``: one JSON file when a path is given, otherwise held in memory."""
    REQUIRES = ()     # a file path or nothing

    def __init__(self, path: Optional[str] = None) -> None:
        self.store = RecipeStore(path)

    def match(self, request: str) -> Sequence[Any]:
        return self.store.match(request)

    def learn(self, request: str, tool: str, args: Mapping[str, Any]) -> Optional[Recipe]:
        return self.store.learn(request, tool, dict(args))


@solution("core-ml-tasks", kind="event", entry="classify", example="Save a note that the launch moves to Friday.")
@typing.runtime_checkable
class MlTasks(typing.Protocol):
    """Classifies a request for the tasks the loop treats as machine learning problems; today, which route it takes."""

    def classify(self, message: str) -> str: ...


@implementation("core-ml-tasks", "router_labels")
class RouterLabels:
    """The classification is the route the loop's planner decides: ``answer``, ``tool``, ``deep``, ``confirm`` or ``clarify``."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def classify(self, message: str) -> str:
        return str(route_message(self.model, message)["route"])


@solution("reason-and-answer", kind="event", entry="reason", example={"prompt": "Reply with the single word: ok", "tier": "fast"})
@typing.runtime_checkable
class StepReasoning(Reasoning, typing.Protocol):
    """The hub's ``reason-and-answer`` interface: a prompt asked of the model at a measured tier, and the part of a step of the loop."""


@implementation("reason-and-answer", "rebuilt_per_step")
class RebuiltPerStep:
    """Each step is a fresh planner call built from the template, the cached head and context layers and the newest observations.
    ``reason`` asks the model chosen for the tier (a tier the chooser does not list is passed on as the model name); the other
    methods hand a message to the member that owns that part of a step."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: RoutedModelCalling, tiers: RegistryTierSelection, prompts: Lazy[PromptAssembling], turns: Lazy[TurnAnswering],
                 replays: Lazy[OperationReplaying], labels: Lazy[MlTasks], whole: Lazy[EndToEnd], steps: Lazy[InterimPolicy],
                 work: Lazy[WorkShowing]) -> None:
        self.model, self.tiers, self.prompts, self.turns = model, tiers, prompts, turns
        self.replays, self.labels, self.whole, self.steps, self.work = replays, labels, whole, steps, work

    def reason(self, prompt: str, tier: str = "") -> str:
        arm = (self.tiers.choose().get(HUB_TIER_OF.get(tier, tier)) or {}).get("arm") if tier else None
        return self.model.answer([{"role": "user", "content": prompt}], model=arm[1] if arm else tier)

    def step_prompt(self, message: str) -> str:
        return self.prompts.assemble(message)

    def turn(self, message: str) -> GoalAnswer:
        return self.turns.turn(message)

    def end_to_end(self, message: str) -> GoalAnswer:
        return self.whole.answer(message)

    def replayable(self, request: str) -> Sequence[Any]:
        return self.replays.match(request)

    def classify(self, message: str) -> str:
        return self.labels.classify(message)

    def interim(self, state: InterimState) -> InterimDecision:
        return self.steps.decide(state)

    def trail(self, emit: Any, context_meta: Optional[Mapping[str, Any]]) -> Any:
        return self.work.start(emit, context_meta)
