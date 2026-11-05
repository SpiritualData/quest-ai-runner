"""World: how the assistant learns what is outside it and acts on it, each line handed to the member that owns it.

The composition arm ``composition`` takes the World members as typed seats, built on first use: Model Plumbing asks the model through one
measured path, Science answers from public research and Tools says where a request is carried out.
"""
from __future__ import annotations

from typing import Any, Mapping

from esoi import Lazy, implementation
from hubofhubs.api import ModelPlumbing

from quest_ai_runner.the_body.hands.quest_operations.interface import ReachClaiming
from quest_ai_runner.the_world.science.interface import KnowingTheWorld


@implementation("the-world", "composition")
class WorldFlow:
    """Research and reach: the outside world answered from public research, and a request placed inside or outside the readable sources."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, models: Lazy[ModelPlumbing], science: Lazy[KnowingTheWorld], tools: Lazy[ReachClaiming]) -> None:
        self.models, self.science, self.tools = models, science, tools

    def ask(self, prompt: str, tier: str = "") -> str:
        return self.models.ask(prompt, tier)

    def research(self, queries: Any, **options: Any) -> Any:
        return self.science.search(queries, **options)

    def claim(self, message: str) -> Mapping[str, Any]:
        return self.tools.claim(message)
