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
from esoi.requirements import api, env
from hubofhubs.seats import CardStore, ModelCalling, Remembering

from .area_reason_and_answer import StepReasoning
from .config import RunnerConfig, build_orchestrator
from .loop_wiring import GoalAnswer, NoContext, orchestrator_over  # noqa: F401  (re-exported: the names callers import from here)
from .runner.quest_client import QuestClient


@solution("access-quest-operations", kind="event", entry="list_quest_goals", example="quest-id")
@typing.runtime_checkable
class QuestOperations(typing.Protocol):
    """The Quest account the runner works in: reads a quest's goals, and carries out the operations a person asked for."""

    def list_quest_goals(self, quest_id: str, **options: Any) -> Any: ...


@implementation("access-quest-operations", "quest_client")
def quest_client(base_url: str, api_key: str, team_id: str = "") -> QuestClient:
    """The Quest HTTP client for the account named by ``QUEST_BASE_URL`` (or ``QUEST_API_URL``) and ``QUEST_API_KEY``."""
    return QuestClient(base_url=base_url, api_key=api_key, team_id=team_id or None)


quest_client.REQUIRES = (api("Quest", key=env("QUEST_API_KEY", option="api_key", secret=True)),
                         env("QUEST_BASE_URL", option="base_url", also=("QUEST_API_URL",)),
                         env("QUEST_TEAM_ID", option="team_id", required=False))


@solution("do-grounded-work", kind="event", entry="answer", example="What should I work on first this week?")
@typing.runtime_checkable
class GoalAgent(typing.Protocol):
    """Answers a person's message from their own quests and cards, and does the work they ask for."""

    def answer(self, message: str) -> GoalAnswer: ...


@implementation("do-grounded-work", "library")
class QuestAiRunnerLibrary:
    """The orchestrator of ``core/orchestrator.py`` wired to the seats it is handed; one object answers any number of messages. The
    ``reasoning`` area is built on first use and not read by ``answer``, which reaches the loop's parts through the orchestrator."""

    def __init__(self, model: ModelCalling, cards: CardStore, memory: Remembering, quest: QuestOperations,
                 reasoning: Lazy[StepReasoning]) -> None:
        self.model, self.cards, self.memory, self.quest, self.reasoning = model, cards, memory, quest, reasoning

    def config(self) -> RunnerConfig:
        """The runner's configuration over the seats: the model, the cards as the context assembler, the Quest account."""
        return RunnerConfig(quest_base_url=getattr(self.quest, "base_url", ""), quest_api_key=getattr(self.quest, "api_key", ""),
                            team_id=getattr(self.quest, "team_id", "") or "", model_provider=self.model,
                            context_assembler=self.memory)

    def answer(self, message: str) -> GoalAnswer:
        result = build_orchestrator(self.config()).run(message)
        return GoalAnswer(text=result.text or result.question or "", kind=result.kind, exit_reason=result.exit_reason,
                          ran=tuple(result.goals), question=result.question or "", steps=result.steps)
