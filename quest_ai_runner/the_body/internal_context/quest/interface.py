"""The interface of QuestOperations: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any
from esoi import solution


@solution("access-quest-operations", kind="event", entry="list_quest_goals", example="quest-id")
@typing.runtime_checkable
class QuestOperations(typing.Protocol):
    """The Quest account the runner works in: reads a quest's goals, and carries out the operations a person asked for."""

    def list_quest_goals(self, quest_id: str, **options: Any) -> Any: ...
