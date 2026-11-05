"""The interfaces of TurnAnswering, EndToEnd, MlTasks, StepReasoning: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from quest_ai_runner.the_self.interface import GoalAnswer
from hubofhubs.api import Reasoning


@typing.runtime_checkable
class TurnAnswering(typing.Protocol):
    """Answers one chat turn: the loop plans, gathers, re-plans and answers, hands deep work on or asks to confirm."""

    def turn(self, message: str) -> GoalAnswer: ...


@typing.runtime_checkable
class EndToEnd(typing.Protocol):
    """The path as shipped, from a message to the finished answer in one call: what the end-to-end metric measures."""

    def answer(self, message: str) -> GoalAnswer: ...


@typing.runtime_checkable
class MlTasks(typing.Protocol):
    """Classifies a request for the tasks the loop treats as machine learning problems; today, which route it takes."""

    def classify(self, message: str) -> str: ...


@solution("the-mind", kind="event", entry="think", example={"request": "What should I work on first this week?"})
@typing.runtime_checkable
class StepReasoning(Reasoning, typing.Protocol):
    """The hub's ``the-mind`` interface: one request thought through, and the part of a step of the loop."""
