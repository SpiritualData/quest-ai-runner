"""The interface of GoalIteration: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

from typing import Optional, Protocol, Sequence
from esoi import solution
from esoi import MetricMeter
from esoi import SliceSource, SpecSlice


@solution("esoi-application", kind="event", entry="slice_for", example="Tidy the card store")
class GoalIteration(Protocol):
    """What the code adds around one deep goal: a slice source (None for none) and the meters that read its metric."""
    name: str
    measures: bool
    source: Optional[SliceSource]
    meters: Sequence[MetricMeter]

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        """The slice of the specification the goal serves, or None when the arm gives the brief none."""
