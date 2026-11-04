"""The whole runner as one function: a message in, what the runner answered and what ran out.

``do-grounded-work`` is the solution the repository exists for. ``GoalAgent.answer(message)`` runs the orchestrator's bounded loop (plan, gather,
re-plan, answer or hand deep work on) over the three things it cannot do without: a model to ask (``call-a-model``), the cards it
remembers (``card-store`` through ``remember-and-recall``) and the Quest account it works in (``access-quest-operations``). Each is
built by the selected arm of its seat, so ``quest_ai_runner.run("quest-ai-runner", message)`` needs nothing passed: a seat that
lacks a key or a program raises ``Pending`` naming it, and a caller overrides only the seat that differs.
"""
from __future__ import annotations

import typing
from typing import Any

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.seats import CardStore

from .area_reason_and_answer import StepReasoning
from .core.planner_context import CardRecall
from .config import RunnerConfig, build_orchestrator
from .loop_wiring import GoalAnswer
from .message_routing import RequestTakingIn
from .area_act_as_the_body import BodyActing
from .hold_the_intents.area import IntentHolding
from .keep_it_generic.area import KeepingGeneric
from .know_the_world.area import KnowingTheWorld
from .select_context.area import ContextChoosing
from .think_critically.area import MetricHolding
from .watch_for_risk.area import RunWatching
from .providers import RoutedModelCalling
from .runner.quest_client import QuestOperations


@solution("do-grounded-work", kind="event", entry="answer", example="What should I work on first this week?")
@typing.runtime_checkable
class GoalAgent(typing.Protocol):
    """Answers a person's message from their own quests and cards, and does the work they ask for."""

    def answer(self, message: str) -> GoalAnswer: ...


@implementation("do-grounded-work", "library")
class QuestAiRunnerLibrary:
    """The orchestrator of ``core/orchestrator.py`` wired to the seats it is handed; one object answers any number of messages. The
    ``reasoning``, ``taking_in``, ``body``, ``critic``, ``risk``, ``intents``, ``generic``, ``world`` and ``context`` areas are built on first use and not read by ``answer``, which reaches the loop's parts through the
    orchestrator."""

    def __init__(self, model: RoutedModelCalling, cards: CardStore, memory: CardRecall, quest: QuestOperations,
                 reasoning: Lazy[StepReasoning], taking_in: Lazy[RequestTakingIn], body: Lazy[BodyActing], critic: Lazy[MetricHolding],
                 risk: Lazy[RunWatching], intents: Lazy[IntentHolding],
                 generic: Lazy[KeepingGeneric], world: Lazy[KnowingTheWorld], context: Lazy[ContextChoosing]) -> None:
        self.model, self.cards, self.memory, self.quest, self.reasoning, self.taking_in = model, cards, memory, quest, reasoning, taking_in
        self.body, self.critic, self.risk, self.intents, self.generic, self.world = body, critic, risk, intents, generic, world
        self.context = context

    def config(self) -> RunnerConfig:
        """The runner's configuration over the seats: the model, the cards as the context assembler, the Quest account."""
        return RunnerConfig(quest_base_url=getattr(self.quest, "base_url", ""), quest_api_key=getattr(self.quest, "api_key", ""),
                            team_id=getattr(self.quest, "team_id", "") or "", model_provider=self.model,
                            context_assembler=self.memory)

    def answer(self, message: str) -> GoalAnswer:
        result = build_orchestrator(self.config()).run(message)
        return GoalAnswer(text=result.text or result.question or "", kind=result.kind, exit_reason=result.exit_reason,
                          ran=tuple(result.goals), question=result.question or "", steps=result.steps)
