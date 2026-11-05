"""The interfaces of Quest Consciousness (the lines that align the assistant to one Quest user) and Hand Off A Goal. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from esoi import solution

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_intelligence.higher_mind.guidance.interface import GuidanceCard


@solution("hand-off-a-goal", kind="event", entry="assign",
          example={"outcome": "Ship the launch", "title": "Draft the launch note", "description": "", "members": []})
@typing.runtime_checkable
class GoalHandingOff(typing.Protocol):
    """Decides which person of a quest a new goal belongs to and whether an AI can do it itself: ``{"assignee", "ai_can_do"}``."""

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]: ...


@solution("quest-consciousness", kind="event", entry="take_in", example="team-1")
@typing.runtime_checkable
class ConsciousnessFlowing(typing.Protocol):
    """One Quest user's alignment, each line handed to the member that owns it: ``take_in`` the tasks queued for a team, ``rep`` the representative
    a task is for, ``assign`` who a new goal belongs to, ``escalate`` a decision to the person it belongs to, ``standing`` the rules the person
    corrected, ``guide`` the guidance that applies to a message, ``suggest`` what is likely to work for a quest and ``prioritize`` the order of
    work, asks or goals."""

    def take_in(self, team_id: str = "") -> List[str]: ...
    def rep(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]: ...
    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def escalate(self, escalation: Escalation) -> str: ...
    def standing(self, quest_id: str) -> Any: ...
    def guide(self, message: str) -> List[GuidanceCard]: ...
    def suggest(self, quest: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def prioritize(self, person: str = "", request: str = "", candidates: Sequence[Mapping[str, Any]] = (), stated: Any = None, now: str = "",
                   subject: str = "work", batch_limit: int = 0) -> Mapping[str, Any]: ...
