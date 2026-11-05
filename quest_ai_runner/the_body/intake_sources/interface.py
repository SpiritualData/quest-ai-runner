"""The interface of ContextFeeding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any
from datetime import datetime
from typing import Any, Dict, Optional, Sequence
from esoi import solution
from quest_ai_runner.the_body.intake_sources.context_update_bundle import ContextUpdates


@solution("feed-context-sources", kind="event", entry="collect", example={"quest_id": "quest-id"})
@typing.runtime_checkable
class ContextFeeding(typing.Protocol):
    """What changed on a card since it was last looked at, gathered from every channel the card watches into one bundle."""

    def collect(self, card: Dict[str, Any], *, card_id: str = "", card_kind: str = "quest", since: Optional[datetime] = None,
                card_label: str = "", sources: Optional[Sequence[str]] = None,
                options: Optional[Dict[str, Dict[str, Any]]] = None) -> ContextUpdates: ...
