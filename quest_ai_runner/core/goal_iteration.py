"""The arms of ``iterate-by-spec``: how much the code does around a deep goal beyond what the standing instructions say.

Three alternatives, one port. The metric record itself (the ledger, ``record_metric_result``, the check that a changed run has
one, the ``metrics`` payload) is not an alternative: it is how the doctrine's own demand, "what the relevant metric result was
before and after your change", is verified, so it runs under every arm (``goal_metrics``). The arms differ in what the code
contributes:

  ``generic-doctrine``      nothing. The standing instructions are the whole method; the worker's recorded results, or its
                            statement that none applies, are what the record holds. The off switch.
  ``slice-only``            the slice of the specification the goal serves goes into the brief: the next milestone and the
                            metric that measures it (``esoi.spec_slice``).
  ``slice-and-writeback``   the slice, plus the runner reads the metric before the work and after it with its own meters
                            (``esoi.metric_meter``) and writes those readings to the ledger itself. The selected arm.

``slice-and-writeback`` is selected by Joshua's direction of 2026-10-21 that every run's metric result before and after is
recorded by code; the replay metric of the solution is not built, so that is a direction and not a measured win. A lane turns
it off with ``ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC=generic-doctrine`` or its ``selections``. Selection goes through
``resolve("iterate-by-spec")`` like every swappable part, and ``Orchestrator`` falls back to it when it is built without an
arm, so a consumer that builds the brain itself gets the same default as ``build_orchestrator``.

A repository without ESOI records, or a goal whose working folder is not known, has no slice, and an arm that measures then
reads nothing, announces nothing and waits for nothing. When there is a slice, a reading is bounded (``read_value``): it is
announced before a meter that takes time runs, and a meter that has not answered in time is left behind.

An arm is only what the goal loop hands the ``esoi.iteration.MetricIteration`` it runs: a slice ``source`` (or none) and the
``meters`` it reads with. The reading, the ledger and the bound on both are the esoi library's.
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence

from esoi import implementation
from esoi.metric_meter import MetricMeter, meters_from_names
from esoi.spec_slice import RepoSliceSource, SliceSource

from . import repo_specs
from .runtime_settings import core_settings, process_environment


class GoalIteration(Protocol):
    """What the code adds around one deep goal: a slice source (None for none) and the meters that read its metric."""
    name: str
    measures: bool
    source: Optional[SliceSource]
    meters: Sequence[MetricMeter]


@implementation("iterate-by-spec", "generic-doctrine")
class DoctrineOnly:
    """The standing instructions are the whole method: no slice in the brief and no reading by the runner."""
    name = "generic-doctrine"
    measures = False
    source: Optional[SliceSource] = None
    meters: Sequence[MetricMeter] = ()


@implementation("iterate-by-spec", "slice-only")
class SliceOnly(DoctrineOnly):
    """The brief gets the slice for the solution the goal serves: its next milestone and the metric that measures it."""
    name = "slice-only"

    def __init__(self, source: Optional[SliceSource] = None):
        self.source = source or RepoSliceSource(find_root=repo_specs.slice_root, cache_dir=repo_specs.cache_dir())


@implementation("iterate-by-spec", "slice-and-writeback")
class SliceAndWriteback(SliceOnly):
    """The slice, and the runner's own readings of its metric before and after the work, written to the run's record."""
    name = "slice-and-writeback"
    measures = True

    def __init__(self, source: Optional[SliceSource] = None, meters: Optional[Sequence[MetricMeter]] = None):
        super().__init__(source)
        self.meters = list(meters) if meters is not None else meters_from_names(core_settings().metric_meters,
                                                                               process_environment())
