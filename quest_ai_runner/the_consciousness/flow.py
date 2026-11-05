"""Quest Consciousness: the parts that align the assistant to one Quest user, handed their line each.

The composition arm ``composition`` takes the Consciousness members as typed seats, built on first use: Quest Representatives chooses the
representative a task is for, Quest Decision Requests carries a decision to the person it belongs to, Feedback Learning keeps what the
person corrected and Intent Evolution suggests what is most likely to work for their intents.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from esoi import Lazy, implementation

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_consciousness.feedback_learning.feedback_ledger import FeedbackKeeping
from quest_ai_runner.the_consciousness.intent_evolution.interface import IntentEvolving
from quest_ai_runner.the_consciousness.judgment_calls.interface import EscalationSink
from quest_ai_runner.the_consciousness.representatives.interface import RepSelecting


@implementation("quest-consciousness", "composition")
class ConsciousnessFlow:
    """One Quest user's alignment: each line is handed to the member that owns it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, reps: Lazy[RepSelecting], decisions: Lazy[EscalationSink], feedback: Lazy[FeedbackKeeping],
                 evolution: Lazy[IntentEvolving]) -> None:
        self.reps, self.decisions, self.feedback, self.evolution = reps, decisions, feedback, evolution

    def rep(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
        return self.reps.resolve(task)

    def escalate(self, escalation: Escalation) -> str:
        return self.decisions.escalate(escalation)

    def standing(self, quest_id: str) -> Any:
        return self.feedback.standing_rules(quest_id)

    def suggest(self, quest: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.evolution.suggest(quest)
