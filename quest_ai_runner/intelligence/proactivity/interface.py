"""The interface of PassGating: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Mapping
from esoi import solution


@solution("quest-autopilot", kind="event", entry="gate", example="quest-id")
@typing.runtime_checkable
class PassGating(typing.Protocol):
    """Keeps background work inside its hours and budget: whether one run of a named autopilot is skipped, and why, or passes every gate to the work.

    When a run is due is the backend's scheduler asking the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``) and what matters first among
    the work waiting is the hub's Prioritization (``quest_ai_runner.run("prioritization", ...)``, answered by Quest's ordering arm). The gate adds only
    what the runner alone sees: the allowance for automatic extra runs, the plan's cap and the pauses, backpressure per autopilot and open holds."""

    def gate(self, quest_id: str, now: str = "", autopilot_id: str = "") -> Mapping[str, Any]: ...
