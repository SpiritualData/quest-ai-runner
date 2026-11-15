"""The interface of QuestPlacing: a Protocol declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping, Optional
from esoi import solution


@solution("place-on-a-quest", kind="event", entry="place", example={"message": "lets get the beta launch moving", "previous": ""})
@typing.runtime_checkable
class QuestPlacing(typing.Protocol):
    """Which of the person's quests a message belongs to, or none. The host (the Quest backend) owns the person's quests and supplies the
    function that places a message on one; a standalone runner uses the library judge over the quests it was given."""

    def place(self, message: str, previous: str = "", home_quest_id: Optional[str] = None, max_quests: int = 1) -> Mapping[str, Any]:
        """``{"picked": [quest ids], "ranked": [quest ids, best first]}``. ``picked`` is empty when no quest fits: the caller then asks the
        person one short question instead of guessing. A quest the person chose explicitly (a pinned conversation) is never placed: the pin stays."""
        ...
