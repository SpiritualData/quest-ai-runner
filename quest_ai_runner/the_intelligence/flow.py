"""Intelligence: one request turned into a response, each line handed to the member that owns it.

The composition arm ``composition`` takes the Intelligence members as typed seats, built on first use: Quest Intents says what the person is
working toward, Context Retriever chooses the context the request needs, Mind reasons through the request and Critical Thinking holds the work
to its measure.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from esoi import Lazy, implementation
from hubofhubs.api import AssembledContext

from quest_ai_runner.the_intelligence.critical_thinking.interface import MetricHolding
from quest_ai_runner.the_intelligence.higher_mind.objectives.interface import IntentHolding
from quest_ai_runner.the_intelligence.mind.interface import StepReasoning
from quest_ai_runner.the_intelligence.subconscious.context_retriever.interface import ContextSelecting


@implementation("the-intelligence", "composition")
class IntelligenceFlow:
    """A request read against the person's intents, its context chosen, thought through and held to a measure."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, intents: Lazy[IntentHolding], context: Lazy[ContextSelecting], reasoning: Lazy[StepReasoning],
                 critic: Lazy[MetricHolding]) -> None:
        self.intents, self.context, self.reasoning, self.critic = intents, context, reasoning, critic

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]:
        return self.intents.hold(quest_id, message)

    def choose(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return self.context.assemble(task_text, meta=meta)

    def think(self, request: str, **inputs: Any) -> Mapping[str, Any]:
        return self.reasoning.think(request, **inputs)

    def measure(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.critic.hold(case)
