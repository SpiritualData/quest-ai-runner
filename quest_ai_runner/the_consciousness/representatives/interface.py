"""The interface of RepSelecting: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping, Optional, Tuple
from esoi import solution


@solution("select-a-rep", kind="event", entry="resolve", example={"assignee_rep_id": "rep-1"})
@typing.runtime_checkable
class RepSelecting(typing.Protocol):
    """Chooses the one AI representative a task is for: the persona id and its skill folder, or nothing when the task names none."""

    def resolve(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]: ...
