"""The interface of MetricHolding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution
from hubofhubs.api import CriticalThinking


@solution("critical-thinking", kind="event", entry="evaluate",
          example={"request": "Ship the launch note", "outcome": {"kind": "deep", "has_metric": True, "changed": True, "before": 0.4, "after": 0.6}})
@typing.runtime_checkable
class MetricHolding(CriticalThinking, typing.Protocol):
    """The hub's ``critical-thinking`` interface (translate a request into a target, evaluate the outcome against it) and QAR's line that holds
    a request to the metric that serves it: the rows of its reading before and after that the code kept, whether a change ended with none."""

    def hold(self, case: Mapping[str, Any]) -> Mapping[str, Any]: ...
