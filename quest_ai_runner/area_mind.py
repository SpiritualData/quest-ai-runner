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

import typing
from typing import Any, Callable, List, Mapping, Optional, Sequence

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.the_world.model_plumbing.vendor_model_caller.interface import ModelCalling
from hubofhubs.the_intelligence.mind.interface import Reasoning

from .core.model_registry import HUB_TIER_OF, RegistryTierSelection
from .core.interim_policy import InterimDecision, InterimPolicy, InterimState
from .core.recipes import Recipe, RecipeStore
from .core.work_trace import WorkShowing
from .loop_wiring import GoalAnswer, orchestrator_over
from .message_routing import route_message


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


@solution("the-mind", kind="event", entry="think", example={"request": "What should I work on first this week?"})
@typing.runtime_checkable
class StepReasoning(Reasoning, typing.Protocol):
    """The hub's ``the-mind`` interface: one request thought through, and the part of a step of the loop."""


@implementation("the-mind", "rebuilt_per_step")
class RebuiltPerStep:
    """Each step is a fresh planner call built from the template, the cached head and context layers and the newest observations.
    ``think`` runs one turn of that loop (its model is a need of the arm, so a missing model program is a pending need, not an error in the middle of a turn) and returns the hub's Mind mapping; the other methods hand a message to the member that owns that
    part of a step."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, prompts: Lazy[PromptAssembling], tiers: Lazy[RegistryTierSelection], turns: TurnAnswering, replays: Lazy[OperationReplaying],
                 labels: Lazy[MlTasks], whole: Lazy[EndToEnd], steps: Lazy[InterimPolicy], work: Lazy[WorkShowing]) -> None:
        self.prompts, self.tiers, self.turns = prompts, tiers, turns
        self.replays, self.labels, self.whole, self.steps, self.work = replays, labels, whole, steps, work

    def think(self, request: str, authority: Optional[Mapping[str, Any]] = None, target: Optional[Mapping[str, Any]] = None,
              guidance: Optional[Mapping[str, Any]] = None, profile: Optional[Any] = None, prepared: Optional[Mapping[str, Any]] = None,
              unmet: Optional[Mapping[str, Any]] = None, outside: Optional[Mapping[str, Any]] = None, budget: Optional[Any] = None,
              cancelled: Optional[Callable[[], bool]] = None) -> Mapping[str, Any]:
        answer = self.turns.turn(request)
        return {"text": answer.text, "results": [{"piece": goal, "worker": "delegated-agent-sessions"} for goal in answer.ran],
                "pieces": list(answer.ran), "steps": answer.steps, "kind": answer.kind, "exit_reason": answer.exit_reason,
                "question": answer.question}

    def starts_on(self, tier: str) -> str:
        """The model a step of the loop starts on for a tier: the one the tier chooser measured best for it (a tier it does not list comes back as given)."""
        arm = (self.tiers.choose().get(HUB_TIER_OF.get(tier, tier)) or {}).get("arm")
        return arm[1] if arm else tier

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
