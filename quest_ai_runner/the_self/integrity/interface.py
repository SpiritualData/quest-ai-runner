"""The interface of KeepingGeneric: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import Genericity


@solution("system-integrity", kind="event", entry="audit", example=["examplevendor"])
@typing.runtime_checkable
class KeepingGeneric(Genericity, typing.Protocol):
    """The hub's ``system-integrity`` interface: the vendor-name branches in QAR's own brain for the terms given, how stale the hub's
    research is and how much of the hub connects by configuration, and the check of a run record."""
