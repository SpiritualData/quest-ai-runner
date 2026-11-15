"""The interface of KnowingTheWorld: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import WorldKnowing


@solution("the-science", kind="event", entry="search", example="meditation and attention")
@typing.runtime_checkable
class KnowingTheWorld(WorldKnowing, typing.Protocol):
    """The hub's ``the-science`` interface: what the world says now, as observations a reader can cite."""
