"""The interface of DecisionJudging: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Callable, Dict, List, Optional, Sequence
from esoi import solution


@solution("judge-a-decision", kind="event", entry="reach_verdict", example='{"reach": "inside", "reason": "the note is in the card"}')
@typing.runtime_checkable
class DecisionJudging(typing.Protocol):
    """Settles a narrow question from a model's raw answer: the reach of a request, the quests an item belongs to, the cards still abridged."""

    def reach_verdict(self, raw: str) -> Optional[Dict[str, Any]]: ...
    def rank(self, items: Sequence[Any], quests: Sequence[Any], call: Callable[[str, Any], str], home_quest_id: Optional[str] = None,
             fallback_order: Optional[Sequence[str]] = None) -> Any: ...
    def unfetched(self, cards: Sequence[Any], executed: Sequence[Any]) -> List[str]: ...
