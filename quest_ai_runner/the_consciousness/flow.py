"""Quest Consciousness: the parts that align the assistant to one Quest user, handed their line each.

The composition arm ``composition`` takes the Consciousness members as typed seats, built on first use: Quest Requests runs the tasks queued
for a team, Quest Representatives chooses the representative a task is for, Hand Off A Goal says who a new goal belongs to, Quest Decision
Requests carries a decision to the person it belongs to, Feedback Learning keeps what the person corrected, Guide By Feedback shows the
corrections that apply, Prioritization (the hub's, with Quest's ordering arm) puts in order the work to start, the asks to put to the person and the person's goals, and Intent
Evolution suggests what is most likely to work for their intents.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Tuple

from esoi import Lazy, implementation
from hubofhubs.api import Prioritizing

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_body.hands.quest_operations.interface import GoalHandingOff
from quest_ai_runner.the_consciousness.feedback_learning.feedback_ledger import FeedbackKeeping
from quest_ai_runner.the_consciousness.intent_evolution.interface import IntentEvolving
from quest_ai_runner.the_consciousness.judgment_calls.interface import EscalationSink
from quest_ai_runner.the_consciousness.representatives.interface import RepSelecting
from quest_ai_runner.the_consciousness.request_intake.interface import QueueRunning
from quest_ai_runner.the_intelligence.higher_mind.guidance.interface import GuidanceCard, GuidanceProvider


@implementation("quest-consciousness", "composition")
class ConsciousnessFlow:
    """One Quest user's alignment: each line is handed to the member that owns it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, queue: Lazy[QueueRunning], reps: Lazy[RepSelecting], handoff: Lazy[GoalHandingOff],
                 decisions: Lazy[EscalationSink], feedback: Lazy[FeedbackKeeping], guidance: Lazy[GuidanceProvider],
                 evolution: Lazy[IntentEvolving], priorities: Lazy[Prioritizing]) -> None:
        self.queue, self.reps, self.handoff, self.decisions = queue, reps, handoff, decisions
        self.feedback, self.guidance, self.evolution, self.priorities = feedback, guidance, evolution, priorities

    def take_in(self, team_id: str = "") -> List[str]:
        return self.queue.scan(team_id)

    def rep(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
        return self.reps.resolve(task)

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.handoff.assign(goal)

    def escalate(self, escalation: Escalation) -> str:
        return self.decisions.escalate(escalation)

    def standing(self, quest_id: str) -> Any:
        return self.feedback.standing_rules(quest_id)

    def guide(self, message: str) -> List[GuidanceCard]:
        return self.guidance.select(message)

    def suggest(self, quest: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.evolution.suggest(quest)

    def prioritize(self, person: str = "", request: str = "", candidates: Sequence[Mapping[str, Any]] = (), stated: Any = None, now: str = "",
                   subject: str = "work", batch_limit: int = 0) -> Mapping[str, Any]:
        return self.priorities.prioritize(person=person, request=request, candidates=candidates, stated=stated, now=now, subject=subject, batch_limit=batch_limit)
