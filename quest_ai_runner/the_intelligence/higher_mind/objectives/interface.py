"""The interfaces of IntentHolding, IntentGrounding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping, Optional
from esoi import solution


@solution("quest-intents", kind="event", entry="hold", example={"quest_id": "quest-id", "message": "What should I work on first this week?"})
@typing.runtime_checkable
class IntentHolding(typing.Protocol):
    """What is already known for a request: the recent cards, the guidance that applies, what is still owed and the predictions."""

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]: ...


@solution("guidance", kind="event", entry="frame", example="quest-id")
@typing.runtime_checkable
class IntentGrounding(typing.Protocol):
    """Ties work to its purpose: the frame of the person's intent a quest sits under, from the ids Quest holds."""

    def frame(self, quest_id: str) -> Optional[Mapping[str, Any]]: ...
