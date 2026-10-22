"""The arms of ``iterate-by-spec``: how much the code does around a deep goal beyond what the standing instructions say.

Three alternatives, one port. The metric record itself (the ledger, ``record_metric_result``, the check that a changed run has
one, the ``metrics`` payload) is not an alternative: it is how the doctrine's own demand, "what the relevant metric result was
before and after your change", is verified, so it runs under every arm (``goal_metrics``). The arms differ in what the code
contributes:

  ``generic-doctrine``      nothing. The standing instructions are the whole method; the worker's recorded results, or its
                            statement that none applies, are what the record holds. This is the selected arm.
  ``slice-only``            the slice of the specification the goal serves goes into the brief: the next milestone and the
                            metric that measures it (``spec_slice``).
  ``slice-and-writeback``   the slice, plus the runner reads the metric before the work and after it with its own meters
                            (``metric_meter``) and writes those readings to the ledger itself.

The records say the slice arms stay proposals until the replay metric of the solution is run by decision, so they are not
the default; a lane turns one on with ``ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC=slice-and-writeback`` or its
``selections``. Selection goes through ``resolve("iterate-by-spec")`` like every swappable part.
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence, Tuple

from esoi import implementation

from .metric_meter import MetricMeter, meters_from_names, read_value
from .runtime_settings import core_settings
from .spec_slice import RepoSliceSource, SliceSource, SpecSlice


class GoalIteration(Protocol):
    """What the code adds around one deep goal. An arm that adds nothing returns ``None`` and ``(None, "")``."""
    name: str
    measures: bool

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        """The slice of the specification the goal serves, or None."""
        ...

    def read_metric(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Tuple[Optional[float], str]:
        """``(value, meter name)`` of the slice's metric now, or ``(None, "")``."""
        ...


@implementation("iterate-by-spec", "generic-doctrine")
class DoctrineOnly:
    """The standing instructions are the whole method: no slice in the brief and no reading by the runner."""
    name = "generic-doctrine"
    measures = False

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        return None

    def read_metric(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Tuple[Optional[float], str]:
        return None, ""


@implementation("iterate-by-spec", "slice-only")
class SliceOnly(DoctrineOnly):
    """The brief gets the slice for the solution the goal serves: its next milestone and the metric that measures it."""
    name = "slice-only"

    def __init__(self, source: Optional[SliceSource] = None):
        self.source = source or RepoSliceSource()

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        return self.source.slice_for(request, working_dir)


@implementation("iterate-by-spec", "slice-and-writeback")
class SliceAndWriteback(SliceOnly):
    """The slice, and the runner's own readings of its metric before and after the work, written to the run's record."""
    name = "slice-and-writeback"
    measures = True

    def __init__(self, source: Optional[SliceSource] = None, meters: Optional[Sequence[MetricMeter]] = None):
        super().__init__(source)
        self.meters = list(meters) if meters is not None else meters_from_names(core_settings().metric_meters)

    def read_metric(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Tuple[Optional[float], str]:
        return read_value(self.meters, spec_slice, working_dir)
