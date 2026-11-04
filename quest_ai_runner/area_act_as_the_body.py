"""``act-as-the-body``: the host's data model is the AI's own consistent state, read when it is asked about and read back after a change.

The area arm ``card_and_reads`` takes the area's members as typed seats and offers the area's one line, ``read(rel_path)``: the part of
a source the host's retrieval adapter returns, as it is now. The members are the cards a quest keeps (``persist-cards``), the host's
reads (``gather-evidence``, the retrieval adapter), the Quest account (``access-quest-operations``) and the operations on it (``operate-a-quest``), the
changes the AI makes to files (``write-files``), what it claims it can reach (``claim-reach``), the queued tasks it runs
(``run-queued-tasks``) and the goals it takes over from a person (``hand-off-a-goal``). Members the line does not need are built on first use.
"""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Mapping, Optional

from esoi import implementation, solution
from esoi.assemble import Lazy
from esoi.requirements import api, env
from hubofhubs.ports.deep import FileWriter
from hubofhubs.recall.card_repository import CardRepository
from hubofhubs.seats import ModelCalling

from .config import RunnerConfig
from .core.adapters_retrieval import RetrievalAdapter
from .runner.quest_client import QuestClient, QuestOperations
from .loop_wiring import GoalAnswer, NoContext, goal_answer, orchestrator_over
from .message_routing import route_message
from .runner.goal_handoff import choose_assignee, make_judge
from .runner.poller import Poller


@implementation("access-quest-operations", "quest_client")
def quest_client(base_url: str, api_key: str, team_id: str = "") -> QuestClient:
    """The Quest HTTP client for the account named by ``QUEST_BASE_URL`` (or ``QUEST_API_URL``) and ``QUEST_API_KEY``."""
    return QuestClient(base_url=base_url, api_key=api_key, team_id=team_id or None)


quest_client.REQUIRES = (api("Quest", key=env("QUEST_API_KEY", option="api_key", secret=True)),
                         env("QUEST_BASE_URL", option="base_url", also=("QUEST_API_URL",)),
                         env("QUEST_TEAM_ID", option="team_id", required=False))


@solution("persist-cards", kind="store", entry="read", example="card-1")
@typing.runtime_checkable
class CardPersisting(CardRepository, typing.Protocol):
    """The cards a quest keeps, written and read back whole (the hub's card repository interface)."""


@solution("write-files", kind="event", entry="read_file", example="NOTES.md")
@typing.runtime_checkable
class FileWriting(FileWriter, typing.Protocol):
    """Changes the text files under one root and keeps what it replaced; a file outside the root is refused."""


@solution("operate-a-quest", kind="event", entry="operate", example="Add a goal to ship the launch note by Friday")
@typing.runtime_checkable
class QuestOperating(typing.Protocol):
    """Carries out what a person asks of their quest (a goal, a habit, a note, an outcome) with the Quest tools and reports what was done."""

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer: ...


def quest_config(model: Any, quest: Any) -> RunnerConfig:
    """The runner's configuration over a model and the Quest account that holds the person's quest."""
    return RunnerConfig(quest_base_url=getattr(quest, "base_url", ""), quest_api_key=getattr(quest, "api_key", ""),
                        team_id=getattr(quest, "team_id", "") or "", model_provider=model, context_assembler=NoContext())


@implementation("operate-a-quest", "qar_orchestrator")
class OrchestratorOperating:
    """The orchestrator over the model and the Quest account: the planner picks a Quest tool, the registry runs it and the reply reports it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: ModelCalling, quest: QuestOperations) -> None:
        self.model, self.quest = model, quest

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer:
        from .config import build_orchestrator
        return goal_answer(build_orchestrator(quest_config(self.model, self.quest)).run(message, quest_id=quest_id or None))


@solution("claim-reach", kind="event", entry="claim", example="Send the launch note to the whole team")
@typing.runtime_checkable
class ReachClaiming(typing.Protocol):
    """Says where a request is carried out: inside the readable sources, by a tool, or outside them by deep work (``answer``, ``tool``,
    ``deep``, ``confirm`` or ``clarify``), from what the host's effectors say they allow."""

    def claim(self, message: str) -> Mapping[str, Any]: ...


@implementation("claim-reach", "coding_agent_prompt_reach")
class PromptReach:
    """The model infers what it can reach from each effector's own description in the planner prompt, and the route its planner decides is the claim.
    A ``deep`` claim is work for ``run-deep-work``, whose ladder includes the one named changes a configured outside tool server accepts
    (``write-through-mcp``); a ``tool`` claim is a call the host's registry runs; the reads are the host's (``gather-evidence``)."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def claim(self, message: str) -> Mapping[str, Any]:
        return route_message(self.model, message)


@solution("run-queued-tasks", kind="event", entry="scan", example="team-1")
@typing.runtime_checkable
class QueueRunning(typing.Protocol):
    """Takes the tasks queued for a team, runs each one and reports what happened; returns the ids it handled."""

    def scan(self, team_id: str = "") -> List[str]: ...


@implementation("run-queued-tasks", "qar_runner")
class QarRunner:
    """The poller over the runner's configuration: one scan claims what is queued, runs it and reports it back to Quest."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: ModelCalling, quest: QuestOperations) -> None:
        self.model, self.quest = model, quest

    def scan(self, team_id: str = "") -> List[str]:
        config = quest_config(self.model, self.quest)
        if team_id:
            config.team_id = team_id
        return Poller(config, state_path=None, client=self.quest).run_once()


@solution("hand-off-a-goal", kind="event", entry="assign",
          example={"outcome": "Ship the launch", "title": "Draft the launch note", "description": "", "members": []})
@typing.runtime_checkable
class GoalHandingOff(typing.Protocol):
    """Decides which person of a quest a new goal belongs to and whether an AI can do it itself: ``{"assignee", "ai_can_do"}``."""

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]: ...


@implementation("hand-off-a-goal", "model_judge")
class ModelJudge:
    """One small model call per goal decides the person and whether the AI can do it; a missing or failed judge leaves the goal with the quest owner."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]:
        return choose_assignee(make_judge(self.model), list(goal.get("members") or []), outcome=str(goal.get("outcome") or ""),
                               title=str(goal.get("title") or ""), description=str(goal.get("description") or ""),
                               current_state=str(goal.get("current_state") or ""), preferences=str(goal.get("preferences") or ""))


@solution("act-as-the-body", kind="event", entry="read", example="NOTES.md")
@typing.runtime_checkable
class BodyActing(typing.Protocol):
    """Reads the part of a source the host holds as it is now, and carries out and reads back the changes the AI makes to it."""

    def read(self, rel_path: str) -> Any: ...


@implementation("act-as-the-body", "card_and_reads")
class CardAndReads:
    """A card per quest grounds each turn and the planner reads live through the host's retrieval adapter when it wants more. ``read``
    returns the part of a source as it is now; the other methods hand a request to the member that owns that part of the body."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, cards: Lazy[CardPersisting], evidence: Lazy[RetrievalAdapter], operations: Lazy[QuestOperating],
                 reach: Lazy[ReachClaiming], files: Lazy[FileWriting], quest: QuestOperations, queue: Lazy[QueueRunning],
                 handoff: Lazy[GoalHandingOff]) -> None:
        self.cards, self.evidence, self.operations, self.reach = cards, evidence, operations, reach
        self.files, self.quest, self.queue, self.handoff = files, quest, queue, handoff

    def read(self, rel_path: str) -> Any:
        return self.evidence.read_section(rel_path)

    def card(self, card_id: str) -> Optional[Dict[str, Any]]:
        return self.cards.read(card_id)

    def goals(self, quest_id: str) -> Any:
        return self.quest.list_quest_goals(quest_id)

    def write(self, rel_path: str, content: str) -> Any:
        return self.files.write_file(rel_path, content)

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer:
        return self.operations.operate(message, quest_id)

    def claim(self, message: str) -> Mapping[str, Any]:
        return self.reach.claim(message)

    def run_queue(self, team_id: str = "") -> List[str]:
        return self.queue.scan(team_id)

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.handoff.assign(goal)
