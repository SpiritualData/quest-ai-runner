"""Ports for deep work and its outputs that carry the consumer's own value objects: the escalation sink.

The deep-runner, writer and progress-sink ports moved to the hub (``hubofhubs.ports.deep``); importers name the hub module.
Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from hubofhubs.ports.deep import DeepResult

from .adapters_values import Escalation


@runtime_checkable
class EscalationSink(Protocol):
    """Raise a human-only confirm/decision and return its id."""

    def escalate(self, escalation: Escalation) -> str:
        """Create a decision-request (or equivalent) and return its decision id."""


@runtime_checkable
class StagedPlanRunner(Protocol):
    """A deep runner that can realise a staged plan (``core/orchestrator_staged_plan.py``) its own way.

    For example as queued tasks with a real wait between stages. The orchestrator checks it with
    ``isinstance`` (a renamed method is then a failed check, not a silent fall back to the in-process
    run, which a consumer's runner may not be able to honour).

    ``stages`` is ONE plan: an ordered list of stages, each a list of ``{"goal", "brief", "run_when"}``
    tasks that may run in parallel, a stage starting once the one before it has finished
    (``run_when``: ``always``, ``prev_met`` or ``prev_not_met``). The orchestrator calls it once per
    top-level sequence of the planner's plan. Return the DeepResult for the hand-off; return None only
    to hand the plan back untouched, which runs it in process (honoured for the first plan only).
    """

    def run_staged_plan(self, *, goal: str, stages: List[List[Dict[str, Any]]],
                        context_preamble: Optional[str] = None) -> Optional[DeepResult]:
        """Take the plan, or hand it back with None."""
