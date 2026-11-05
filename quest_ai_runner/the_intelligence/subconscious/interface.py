"""The interface of CardRecall: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import Remembering


@solution("the-subconscious", kind="event", entry="recall", example="rust release")
@typing.runtime_checkable
class CardRecall(Remembering, typing.Protocol):
    """What a request recalls: the hub's ``the-subconscious`` interface (``recall``), which the loop also reads as its context
    assembler (``assemble`` and ``record``)."""
