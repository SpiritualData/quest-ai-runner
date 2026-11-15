"""The interface of CardRecall: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import AssembledContext, Remembering


@solution("the-subconscious", kind="event", entry="recall", example="rust release")
@typing.runtime_checkable
class CardRecall(Remembering, typing.Protocol):
    """What a request recalls: the hub's ``the-subconscious`` interface (``recall``), which the loop also reads as its context
    assembler (``assemble`` and ``record``)."""


@solution("digest-records-for-a-need", kind="event", entry="digest_need", example="What do my notes say about why I sleep badly?")
@typing.runtime_checkable
class NeedDigesting(typing.Protocol):
    """A need over a large body of the person's records, read one source group at a time and handed on as a few cited findings. ``task_text`` is the
    need and ``meta`` carries the ``source_groups`` (what a host supplies), the background it is asked in and any cap by name."""

    def digest_need(self, task_text: str, *, meta: typing.Optional[typing.Dict[str, typing.Any]] = None) -> AssembledContext: ...
