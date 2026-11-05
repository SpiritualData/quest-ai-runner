"""The interface of BodyActing: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any
from esoi import solution
from hubofhubs.api import FormAndContext


@solution("the-body", kind="event", entry="describe", example="")
@typing.runtime_checkable
class BodyActing(FormAndContext, typing.Protocol):
    """The hub's ``the-body`` interface (describe the system's form, keep the run record), and QAR's line that reads the part of a source
    the host holds as it is now."""

    def read(self, rel_path: str) -> Any: ...
