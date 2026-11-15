"""The interface of RunWatching: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import RiskWatching


@solution("nervous-system", kind="event", entry="watch", example="delete the old backup folder")
@typing.runtime_checkable
class RunWatching(RiskWatching, typing.Protocol):
    """The hub's ``nervous-system`` interface: a digest of one step of a run in, at most one risk signal out. It never raises."""
