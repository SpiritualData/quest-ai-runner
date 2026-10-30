"""Ports for deep work and its outputs that carry the consumer's own value objects: the escalation sink.

The deep-runner, writer and progress-sink ports moved to the hub (``hubofhubs.ports.deep``); importers name the hub module.
Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .adapters_values import Escalation


@runtime_checkable
class EscalationSink(Protocol):
    """Raise a human-only confirm/decision and return its id."""

    def escalate(self, escalation: Escalation) -> str:
        """Create a decision-request (or equivalent) and return its decision id."""
