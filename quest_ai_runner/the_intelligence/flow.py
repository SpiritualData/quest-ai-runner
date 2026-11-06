"""Intelligence: one request turned into a response, each line handed to the member that owns it.

The composition arm ``composition`` takes the Intelligence members as typed seats, built on first use: Quest Intents says what the person is
working toward, Subconscious prepares what the request needs, Mind reasons through the request, Nervous System watches the run, Critical
Thinking holds the work to its measure, Proactivity (the hub's) says what standing work is due and Quest Autopilot asks it, asks Prioritization
what matters first among the work waiting and says whether that work may start now.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from esoi import Lazy, implementation
from hubofhubs.api import Proactive

from quest_ai_runner.the_intelligence.critical_thinking.interface import MetricHolding
from quest_ai_runner.the_intelligence.higher_mind.objectives.interface import IntentHolding
from quest_ai_runner.the_intelligence.mind.interface import StepReasoning
from quest_ai_runner.the_intelligence.nervous_system.interface import RunWatching
from quest_ai_runner.the_intelligence.proactivity.interface import PassGating
from quest_ai_runner.the_intelligence.subconscious.interface import CardRecall


@implementation("quest-intelligence", "composition")
class IntelligenceFlow:
    """A request read against the person's intents, its context prepared, thought through, watched and held to a measure."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, intents: Lazy[IntentHolding], recall: Lazy[CardRecall], reasoning: Lazy[StepReasoning],
                 risk: Lazy[RunWatching], critic: Lazy[MetricHolding], autopilot: Lazy[PassGating], proactivity: Lazy[Proactive]) -> None:
        self.intents, self.recall, self.reasoning, self.risk, self.critic, self.autopilot = intents, recall, reasoning, risk, critic, autopilot
        self.proactivity = proactivity

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]:
        return self.intents.hold(quest_id, message)

    def view(self, request: str, person: str = "", objectives: Sequence[Mapping[str, Any]] = (), stated: Optional[Mapping[str, Any]] = None,
             now: str = "") -> Mapping[str, Any]:
        return self.intents.view(request, person, objectives, stated, now)

    def prepare(self, request: str) -> Mapping[str, Any]:
        return self.recall.prepare(request)

    def think(self, request: str, **inputs: Any) -> Mapping[str, Any]:
        return self.reasoning.think(request, **inputs)

    def watch(self, digest: str) -> Any:
        return self.risk.watch(digest)

    def measure(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.critic.hold(case)

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]:
        return self.autopilot.gate(quest_id, now)

    def due(self, schedules: Sequence[Mapping[str, Any]], now: str = "", priorities: Optional[Mapping[str, Any]] = None) -> Sequence[Mapping[str, Any]]:
        return self.proactivity.due(schedules, now, priorities)
