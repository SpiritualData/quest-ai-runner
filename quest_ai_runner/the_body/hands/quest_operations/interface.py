"""The interface of QuestOperating: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from quest_ai_runner.the_self.interface import GoalAnswer


@solution("quest-change-executor", kind="event", entry="operate", example="Add a goal to ship the launch note by Friday")
@typing.runtime_checkable
class QuestOperating(typing.Protocol):
    """Carries out what a person asks of their quest (a goal, a habit, a note, an outcome) with the Quest tools and reports what was done."""

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer: ...
