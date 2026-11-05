"""The interfaces of PromptAssembling, OperationReplaying, MessageRouting: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping, Sequence
from esoi import solution


@solution("context-intake", kind="event", entry="assemble", example="What should I work on first this week?")
@typing.runtime_checkable
class PromptAssembling(typing.Protocol):
    """Writes the prompt a step of the loop sends for a message: the planner's view of it, as text."""

    def assemble(self, message: str) -> str: ...


@solution("planner", kind="event", entry="match", example="Add a note that the launch moves to Friday")
@typing.runtime_checkable
class OperationReplaying(typing.Protocol):
    """Finds the operations learned from earlier requests that a new request repeats, best first, each with its similarity."""

    def match(self, request: str) -> Sequence[Any]: ...


@typing.runtime_checkable
class MessageRouting(typing.Protocol):
    """Decides where one message goes, as data: ``{"route", "tool", "calls", "raw"}``."""

    def route(self, message: str) -> Mapping[str, Any]: ...
