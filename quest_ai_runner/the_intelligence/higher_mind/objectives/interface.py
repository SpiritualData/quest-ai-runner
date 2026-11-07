"""The interfaces of IntentHolding, IntentGrounding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping, Optional, Sequence
from esoi import solution


@solution("quest-intents", kind="event", entry="hold", example={"quest_id": "quest-id", "message": "What should I work on first this week?"})
@typing.runtime_checkable
class IntentHolding(typing.Protocol):
    """What is already known for a request: the recent cards, the guidance that applies, what is still owed and the predictions."""

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]: ...

    def view(self, request: str, person: str = "", objectives: Sequence[Mapping[str, Any]] = (), stated: Optional[Mapping[str, Any]] = None,
             now: str = "") -> Mapping[str, Any]:
        """The long view the hub's Higher Mind takes (its ``view`` signature): the person's goals, each one the milestone of the solution it is for,
        and ``priority``, the order Prioritization gives them for what the person stated."""
        ...


@solution("guidance", kind="event", entry="frame", example="quest-id")
@typing.runtime_checkable
class IntentGrounding(typing.Protocol):
    """Ties work to its purpose: the frame of the person's intent a quest sits under, from the ids Quest holds."""

    def frame(self, quest_id: str) -> Optional[Mapping[str, Any]]: ...

