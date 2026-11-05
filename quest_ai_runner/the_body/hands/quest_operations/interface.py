"""The interfaces of QuestOperating and ReachClaiming: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Mapping
from esoi import solution
from quest_ai_runner.the_self.interface import GoalAnswer


@solution("quest-change-executor", kind="event", entry="operate", example="Add a goal to ship the launch note by Friday")
@typing.runtime_checkable
class QuestOperating(typing.Protocol):
    """Carries out what a person asks of their quest (a goal, a habit, a note, an outcome) with the Quest tools and reports what was done."""

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer: ...


@solution("the-tools", kind="event", entry="claim", example="Send the launch note to the whole team")
@typing.runtime_checkable
class ReachClaiming(typing.Protocol):
    """Says where a request is carried out: inside the readable sources, by a tool, or outside them by deep work (``answer``, ``tool``,
    ``deep``, ``confirm`` or ``clarify``), from what the host's effectors say they allow."""

    def claim(self, message: str) -> Mapping[str, typing.Any]: ...
