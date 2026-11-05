"""``watch-for-risk``: every run is watched through cheap checks, and only a high-risk case is answered.

The area arm ``overseer`` takes the area's members as typed seats and offers the area's one line, ``watch(digest)``: the one small
signal (``proceed``, ``redirect``, ``answer_now``, ``escalate_deep`` or ``escalate_human``) a model reading the run's digest sends, with
QAR's prompt as the policy (the hub's ``RiskWatching`` interface). The member is the hand-off to a person (``escalate-to-a-human``), built
on first use. The digest and the consultation are the functions of ``core/overseer.py``, unchanged.
"""
from __future__ import annotations

import typing
from typing import Any

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.the_intelligence.mind.delegation.overseer.risk import OverseerSignal
from hubofhubs.the_intelligence.nervous_system.interface import RiskWatching

from ..core import overseer
from ..core.adapters_deep import EscalationSink
from ..core.adapters_values import Escalation
from ..providers import RoutedModelCalling


@solution("watch-for-risk", kind="event", entry="watch", example="delete the old backup folder")
@typing.runtime_checkable
class RunWatching(RiskWatching, typing.Protocol):
    """The hub's ``watch-for-risk`` interface: a digest of one step of a run in, at most one small signal out. It never raises."""


@implementation("watch-for-risk", "overseer")
class Overseer:
    """The digest-driven overseer with QAR's prompt: ``watch`` reads a digest with the model seat, ``oversee`` with the provider and model
    passed in; a failure is a degraded ``proceed``. ``escalate`` hands a step only a person may take to the member that files it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: RoutedModelCalling, escalations: Lazy[EscalationSink]) -> None:
        self.model, self.escalations = model, escalations

    def digest(self, **fields: Any) -> str:
        return overseer.build_digest(**fields)

    def oversee(self, provider: Any, model: str, digest: str) -> OverseerSignal:
        return overseer.oversee(provider, model, digest)

    def watch(self, digest: str, model: str = "") -> OverseerSignal:
        return overseer.oversee(self.model, model, digest)

    def escalate(self, escalation: Escalation) -> str:
        return self.escalations.escalate(escalation)
