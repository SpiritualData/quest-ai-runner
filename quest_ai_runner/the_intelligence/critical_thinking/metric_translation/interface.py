"""The interface of MetricDrafting: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution


@solution("draft-a-metric", kind="event", entry="draft", example={"task_text": "Propose this week's value", "card": {}, "context_view": ""})
@typing.runtime_checkable
class MetricDrafting(typing.Protocol):
    """Drafts a missing metric value from the quest's own data for its owner to confirm; one light model call, capped."""

    def draft(self, task_text: str, card: Mapping[str, Any], context_view: str = "") -> Any: ...
