"""World: how the assistant learns what is outside it and acts on it, each line handed to the member that owns it.

The composition arm ``composition`` takes the World members as typed seats, built on first use: Science answers from public research, Tools says
where a request is carried out and Model Plumbing asks the model through one measured path.
"""
from __future__ import annotations

from typing import Any, Mapping

from esoi import Lazy, implementation

from quest_ai_runner.the_body.hands.quest_operations.interface import ReachClaiming
from quest_ai_runner.the_world.science.interface import KnowingTheWorld


@implementation("the-world", "composition")
class WorldFlow:
    """Research and reach: the outside world answered from public research, and a request placed inside or outside the readable sources."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, science: Lazy[KnowingTheWorld], tools: Lazy[ReachClaiming]) -> None:
        self.science, self.tools = science, tools

    def research(self, queries: Any, **options: Any) -> Any:
        return self.science.search(queries, **options)

    def claim(self, message: str) -> Mapping[str, Any]:
        return self.tools.claim(message)
