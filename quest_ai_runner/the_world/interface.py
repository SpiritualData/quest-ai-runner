"""The interface of the Tools seat QAR fills: where a request is carried out. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Mapping

from esoi import solution


@solution("the-tools", kind="event", entry="claim", example="Send the launch note to the whole team")
@typing.runtime_checkable
class ReachClaiming(typing.Protocol):
    """Says where a request is carried out: inside the readable sources, by a tool, or outside them by deep work (``answer``, ``tool``,
    ``deep``, ``confirm`` or ``clarify``), from what the host's effectors say they allow."""

    def claim(self, message: str) -> Mapping[str, typing.Any]: ...
