"""The interface of Quest World (the lines that reach beyond a person's Quest). Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping

from esoi import solution


@solution("quest-world", kind="event", entry="ask", example="latest Rust version")
@typing.runtime_checkable
class WorldFlowing(typing.Protocol):
    """Reaching beyond the person's Quest, each line handed to the member that owns it: ``ask`` a model through the one measured path,
    ``research`` public literature with its sources and ``claim`` where a request is carried out."""

    def ask(self, prompt: str, tier: str = "") -> str: ...
    def research(self, queries: Any, **options: Any) -> Any: ...
    def claim(self, message: str) -> Mapping[str, Any]: ...
