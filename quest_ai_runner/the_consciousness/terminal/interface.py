"""The interfaces of TerminalHelping, PublicSurface: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any
from typing import Any, Dict, Optional
from esoi import solution
from dataclasses import dataclass
from typing import Sequence


@typing.runtime_checkable
class TerminalHelping(typing.Protocol):
    """The small judgements the terminal commands lean on: whether a goal is work an AI can finish."""

    def judge_goal(self, goal: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...


@typing.runtime_checkable
class PublicSurface(typing.Protocol):
    """Names what the package offers a consumer and hands over the front door asked for (``run``, ``load_config``, ``load_client``)."""

    def front_door(self, name: str) -> Any: ...


@dataclass(frozen=True)
class TerminalRun:
    """What one command of the terminal did: its exit code and everything it printed."""
    code: int
    output: str


@typing.runtime_checkable
class TerminalOperating(typing.Protocol):
    """Runs one command of ``quest-ai-runner`` as the shell would, and returns what it did."""

    def operate(self, argv: Sequence[str], entry: str = "qar") -> TerminalRun: ...
