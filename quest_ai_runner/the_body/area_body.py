"""``the-body``: the host's data model is the AI's own consistent state, read when it is asked about and read back after a change.

QAR's area arm ``card_and_reads`` is the hub's Body for Quest and offers the hub's two lines, ``describe(person)`` (where the system's data
lives, where its requests come from and which enduring integrations it may use: the hub's Internal Context, Intake Sources and Hands,
each built on first use) and ``record_run(run)`` (the run record, kept in the run store, never in the cards), beside QAR's own line,
``read(rel_path)``: the part of a source the host's retrieval adapter returns, as it is now. The members are the cards a quest keeps
(``persist-cards``), the host's reads (``quest-context``, the retrieval adapter), the Quest account (``quest-body``) and the
operations on it (``quest-change-executor``), the changes the AI makes to files (``file-editing``), what it claims it can reach
(``the-tools``), the queued tasks it runs (``quest-requests``) and the goals it takes over from a person (``hand-off-a-goal``).
Members the line does not need are built on first use.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from esoi import implementation
from esoi import Lazy
from esoi import requirements
from hubofhubs.api import BodyComposition
from hubofhubs.api import Connecting
from hubofhubs.api import IntakeSourcing
from hubofhubs.api import InternalContexting
from hubofhubs.api import ModelCalling

from quest_ai_runner.the_body.configuration.config import RunnerConfig
from quest_ai_runner.the_body.internal_context.interface import RetrievalAdapter
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.the_body.internal_context.quest.interface import QuestOperations
from quest_ai_runner.the_self.loop_wiring import NoContext, goal_answer, orchestrator_over
from quest_ai_runner.the_self.interface import GoalAnswer
from quest_ai_runner.the_intelligence.mind.planner.message_routing import route_message
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.interface import RoutedModelCalling
from quest_ai_runner.the_intelligence.higher_mind.objectives.goal_handoff import choose_assignee, make_judge
from quest_ai_runner.the_consciousness.request_intake.poller import Poller
from quest_ai_runner.the_body.hands.quest_operations.interface import QuestOperating
from quest_ai_runner.the_consciousness.request_intake.interface import QueueRunning
from quest_ai_runner.the_body.internal_context.card_store.interface import CardKeeping
from quest_ai_runner.the_world.tools.file_editing.interface import FileWriting
from quest_ai_runner.the_consciousness.interface import GoalHandingOff
from quest_ai_runner.the_world.interface import ReachClaiming


@implementation("quest-body", "quest_client")
def quest_client(base_url: str, api_key: str, team_id: str = "") -> QuestClient:
    """The Quest HTTP client for the account named by ``QUEST_BASE_URL`` (or ``QUEST_API_URL``) and ``QUEST_API_KEY``."""
    return QuestClient(base_url=base_url, api_key=api_key, team_id=team_id or None)


quest_client.REQUIRES = (requirements.api("Quest", key=requirements.env("QUEST_API_KEY", option="api_key", secret=True)),
                         requirements.env("QUEST_BASE_URL", option="base_url", also=("QUEST_API_URL",)),
                         requirements.env("QUEST_TEAM_ID", option="team_id", required=False))



def quest_config(model: Any, quest: Any) -> RunnerConfig:
    """The runner's configuration over a model and the Quest account that holds the person's quest."""
    return RunnerConfig(quest_base_url=getattr(quest, "base_url", ""), quest_api_key=getattr(quest, "api_key", ""),
                        team_id=getattr(quest, "team_id", "") or "", model_provider=model, context_assembler=NoContext())


@implementation("quest-change-executor", "qar_orchestrator")
class OrchestratorOperating:
    """The orchestrator over the model and the Quest account: the planner picks a Quest tool, the registry runs it and the reply reports it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: ModelCalling, quest: QuestOperations) -> None:
        self.model, self.quest = model, quest

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer:
        from quest_ai_runner.the_body.configuration.config import build_orchestrator
        return goal_answer(build_orchestrator(quest_config(self.model, self.quest)).run(message, quest_id=quest_id or None))



@implementation("the-tools", "coding_agent_prompt_reach")
class PromptReach:
    """The model infers what it can reach from each effector's own description in the planner prompt, and the route its planner decides is the claim."""
    REQUIRES = ()     # the model it is handed

    def __init__(self, model: ModelCalling) -> None:
        self.model = model

    def claim(self, message: str) -> Mapping[str, Any]:
        return route_message(self.model, message)



@implementation("quest-requests", "qar_runner")
class QarRunner:
    """The poller over the runner's configuration: one scan claims what is queued, runs it and reports it back to Quest."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: RoutedModelCalling, quest: QuestOperations) -> None:
        self.model, self.quest = model, quest

    def scan(self, team_id: str = "") -> List[str]:
        config = quest_config(self.model, self.quest)
        if team_id:
            config.team_id = team_id
        return Poller(config, state_path=None, client=self.quest).run_once()



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



@implementation("quest-account-truth", "card_and_reads")
class CardAndReads:
    """A card per quest grounds each turn and the planner reads live through the host's retrieval adapter when it wants more. ``describe``
    and ``record_run`` are the hub's Body composition over its Internal Context, Intake Sources and Hands; ``read`` returns the part of a
    source as it is now; the other methods hand a request to the member that owns that part of the body."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, cards: Lazy[CardKeeping], evidence: Lazy[RetrievalAdapter], operations: Lazy[QuestOperating],
                 reach: Lazy[ReachClaiming], files: Lazy[FileWriting], quest: QuestOperations, queue: Lazy[QueueRunning],
                 handoff: Lazy[GoalHandingOff], internal: Lazy[InternalContexting], intake: Lazy[IntakeSourcing],
                 hands: Lazy[Connecting]) -> None:
        self.cards, self.evidence, self.operations, self.reach = cards, evidence, operations, reach
        self.files, self.quest, self.queue, self.handoff = files, quest, queue, handoff
        self.form = BodyComposition(internal, intake, hands)

    def describe(self, person: str = "") -> Mapping[str, Any]:
        return self.form.describe(person)

    def record_run(self, run: Mapping[str, Any]) -> str:
        return self.form.record_run(run)

    def read(self, rel_path: str) -> Any:
        return self.evidence.read_section(rel_path)

    def card(self, card_id: str) -> Optional[Dict[str, Any]]:
        return self.cards.get(card_id)

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
