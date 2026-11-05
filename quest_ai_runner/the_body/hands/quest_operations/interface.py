"""The interfaces of QuestOperating, GoalHandingOff: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution
from hubofhubs.api import OperationWriter
from quest_ai_runner.the_self.interface import GoalAnswer


@solution("quest-change-executor", kind="event", entry="operate", example="Add a goal to ship the launch note by Friday")
@typing.runtime_checkable
class QuestOperating(typing.Protocol):
    """Carries out what a person asks of their quest (a goal, a habit, a note, an outcome) with the Quest tools and reports what was done."""

    def operate(self, message: str, quest_id: str = "") -> GoalAnswer: ...


@solution("hand-off-a-goal", kind="event", entry="assign",
          example={"outcome": "Ship the launch", "title": "Draft the launch note", "description": "", "members": []})
@typing.runtime_checkable
class GoalHandingOff(typing.Protocol):
    """Decides which person of a quest a new goal belongs to and whether an AI can do it itself: ``{"assignee", "ai_can_do"}``."""

    def assign(self, goal: Mapping[str, Any]) -> Mapping[str, Any]: ...


@solution("the-tools", kind="event", entry="claim", example="Send the launch note to the whole team")
@typing.runtime_checkable
class ReachClaiming(typing.Protocol):
    """Says where a request is carried out: inside the readable sources, by a tool, or outside them by deep work (``answer``, ``tool``,
    ``deep``, ``confirm`` or ``clarify``), from what the host's effectors say they allow."""

    def claim(self, message: str) -> Mapping[str, Any]: ...


@solution("write-through-mcp", kind="event", entry="write_operation", example={"name": "issues:create", "args": {"title": "Draft the launch note"}})
@typing.runtime_checkable
class OperationWriting(OperationWriter, typing.Protocol):
    """The hub's operation-writer interface (a named, schema-described mutating operation, reported as a value) over the MCP servers a lane connects."""
