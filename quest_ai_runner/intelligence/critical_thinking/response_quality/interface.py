"""The interface of ReplyGuarding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Callable, List, Mapping
from esoi import solution


@solution("response-quality", kind="event", entry="links", example="See https://example.org/a for details.")
@typing.runtime_checkable
class ReplyGuarding(typing.Protocol):
    """Stops an unsafe reply: finds the links in a text and checks each one, so a link that does not resolve never reaches the person."""

    def links(self, text: str) -> List[Any]: ...
    def sanitize(self, text: str, policy: Mapping[str, Any], fetcher: Callable[[str, float], Any]) -> Any: ...
