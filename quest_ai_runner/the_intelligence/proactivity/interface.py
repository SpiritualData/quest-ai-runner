"""The interface of PassGating: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution


@solution("quest-autopilot", kind="event", entry="gate", example="quest-id")
@typing.runtime_checkable
class PassGating(typing.Protocol):
    """Keeps background work inside its hours and budget: whether a quest's pass is skipped, and why, or passes every gate to the work.

    When work is due is the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``: the quest's cadence, zone, last pass and run request as a
    schedule, its asks as events from the Quest events adapter of Intake Sources) and what matters first among the work waiting is the hub's
    Prioritization (``quest_ai_runner.run("prioritization", ...)``, answered by Quest's ordering arm). The gate adds only what is Quest's own: the
    mode, the daily budget, the roster day, backpressure and open holds."""

    def gate(self, quest_id: str, now: str = "") -> Mapping[str, Any]: ...
