"""The interface of PassGating: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution


@solution("gate-an-autopilot-pass", kind="event", entry="gate", example="quest-id")
@typing.runtime_checkable
class PassGating(typing.Protocol):
    """Keeps background work inside its hours and budget: whether a quest's pass is skipped, and why, or passes every gate to the work."""

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]: ...
