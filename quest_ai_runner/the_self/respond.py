"""The Self's flow, ``respond``: a message in, what the runner answered and what ran out, as a short sequence of the parts that serve it.

``do-grounded-work`` is the solution the repository exists for (``quest_ai_runner.run("quest-ai-runner", message)``). In order: Request intake takes
the request in, Higher mind says what the person is working toward, Subconscious chooses the context the request needs, the Mind reasons step by step
over both (Body acts on the person's Quest account and Science answers what lies beyond their own material), Nervous System watches the run, and
Critical Thinking holds the work to the metric that serves it. The model asked is Vendor Model Caller's, the cards are Subconscious's and the
account is Quest account access: each is built by the selected arm of its seat, so a caller passes only what differs and a seat that lacks a key or a
program raises ``Pending`` naming it.

Today the Mind's selected arm (the orchestrator's bounded loop: plan, gather, re-plan, answer or hand deep work on) hosts every part between intake and
the metric, so ``FLOW`` names where each part runs and ``respond`` makes the one call. A part moves out of the loop into a call of its own
when its entry point beats the loop's inline version on its metric, and ``FLOW`` then names it ``OWN``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Tuple

from esoi import implementation
from esoi import Lazy
from hubofhubs.api import CardStore

from quest_ai_runner.the_body.configuration.config import RunnerConfig, build_orchestrator
from quest_ai_runner.the_body.interface import BodyActing
from quest_ai_runner.the_body.internal_context.quest.interface import QuestOperations
from quest_ai_runner.the_consciousness.intent_evolution.interface import IntentEvolving
from quest_ai_runner.the_consciousness.request_intake.interface import QueueRunning
from quest_ai_runner.the_intelligence.critical_thinking.interface import MetricHolding
from quest_ai_runner.the_intelligence.higher_mind.objectives.interface import IntentHolding
from quest_ai_runner.the_intelligence.mind.interface import StepReasoning
from quest_ai_runner.the_intelligence.nervous_system.interface import RunWatching
from quest_ai_runner.the_intelligence.subconscious.context_retriever.interface import ContextSelecting
from quest_ai_runner.the_intelligence.subconscious.interface import CardRecall
from quest_ai_runner.the_self.integrity.interface import KeepingGeneric
from quest_ai_runner.the_self.interface import GoalAnswer
from quest_ai_runner.the_self.loop_wiring import goal_answer
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.interface import RoutedModelCalling
from quest_ai_runner.the_world.science.interface import KnowingTheWorld

LOOP = "in the Mind's loop"
OWN = "its own call"
PENDING = "no arm yet"   # the interface and its place in the flow exist; the first arm is built after its free rated stage


@dataclass(frozen=True)
class Part:
    """One part of the flow: the solution that serves the request at this point, what it does for it and where it runs today."""
    solution: str
    does: str
    runs: str
    container: str = ""              # the container solution that owns the part, so the flow reads from the top intent down
    reaches: Tuple[str, ...] = ()    # the other containers this part works through while it runs


FLOW: Tuple[Part, ...] = (
    Part("quest-requests", "takes the request in, from a chat turn, a queued task or a live channel", LOOP, "quest-consciousness"),
    Part("quest-intents", "says what the person is working toward, from their quests, goals and intents", LOOP, "quest-intelligence"),
    Part("context-retriever", "chooses the cards, files and conversations the request needs", LOOP, "quest-intelligence"),
    Part("the-mind", "reasons step by step over the request and its context, with Body acting and Science answering beyond the person's own material", OWN,
         "quest-intelligence", ("quest-body", "quest-world")),
    Part("nervous-system", "watches the run for risk before anything irreversible is handed on", LOOP, "quest-intelligence"),
    Part("critical-thinking", "holds the work to the metric that serves it and answers a regression", LOOP, "quest-intelligence"),
    Part("intent-evolution", "suggests the option most likely to work for the person's intents, or an experiment when the best option is unknown", PENDING,
         "quest-consciousness"),
)


@implementation("quest-self", "library")
class Respond:
    """The flow over the seats it is handed; one object answers any number of messages. The ``taking_in``, ``intents``, ``context``, ``reasoning``,
    ``risk``, ``critic``, ``body``, ``world`` and ``generic`` parts are built on first use: ``answer`` reaches them through the Mind's loop."""

    def __init__(self, model: RoutedModelCalling, cards: CardStore, memory: CardRecall, quest: QuestOperations,
                 reasoning: Lazy[StepReasoning], taking_in: Lazy[QueueRunning], body: Lazy[BodyActing], critic: Lazy[MetricHolding],
                 risk: Lazy[RunWatching], intents: Lazy[IntentHolding],
                 generic: Lazy[KeepingGeneric], world: Lazy[KnowingTheWorld], context: Lazy[ContextSelecting],
                 evolution: Lazy[IntentEvolving]) -> None:
        self.model, self.cards, self.memory, self.quest, self.reasoning, self.taking_in = model, cards, memory, quest, reasoning, taking_in
        self.body, self.critic, self.risk, self.intents, self.generic, self.world = body, critic, risk, intents, generic, world
        self.context, self.evolution = context, evolution

    def config(self) -> RunnerConfig:
        """The runner's configuration over the seats: the model, the cards as the context assembler, the Quest account."""
        return RunnerConfig(quest_base_url=getattr(self.quest, "base_url", ""), quest_api_key=getattr(self.quest, "api_key", ""),
                            team_id=getattr(self.quest, "team_id", "") or "", model_provider=self.model,
                            context_assembler=self.memory)

    def answer(self, message: str) -> GoalAnswer:
        turn = goal_answer(build_orchestrator(self.config()).run(message))
        return replace(turn, parts=tuple((part.solution, part.runs) for part in FLOW))
