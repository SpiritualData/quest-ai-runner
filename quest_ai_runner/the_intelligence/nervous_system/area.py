"""``nervous-system``: every run is watched through cheap checks, and only a high-risk case is answered.

The area arm ``overseer`` takes the area's members as typed seats and offers the hub's lines, ``digest(**fields)`` and ``watch(digest)``: the one
``RiskSignal`` (severity none, low, medium or high, and the reason) a model reading the run's digest sends, with QAR's prompt as the policy.
The five-way verdict QAR's loop acts on (``proceed``, ``redirect``, ``answer_now``, ``escalate_deep`` or ``escalate_human``) is the hub's
``RunOverseer`` verdict, a helper of Delegation, kept as QAR's own line ``oversee``. The member is the hand-off to a person
(``escalate-to-a-human``), built on first use. The digest and the consultation are the functions of ``core/overseer.py``, unchanged.
"""
from __future__ import annotations

import typing
from typing import Any

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.api import OverseerSignal, to_risk_signal
from hubofhubs.api import RiskWatching

from quest_ai_runner.the_intelligence.mind.delegation import overseer
from quest_ai_runner.the_consciousness.judgment_calls.interface import EscalationSink
from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.providers import RoutedModelCalling


@solution("nervous-system", kind="event", entry="watch", example="delete the old backup folder")
@typing.runtime_checkable
class RunWatching(RiskWatching, typing.Protocol):
    """The hub's ``nervous-system`` interface: a digest of one step of a run in, at most one risk signal out. It never raises."""


@implementation("nervous-system", "overseer")
class Overseer:
    """The digest-driven overseer with QAR's prompt: ``watch`` reads a digest with the model seat and returns the hub's ``RiskSignal``,
    ``oversee`` returns the five-way verdict with the provider and model passed in; a failure is a degraded ``proceed`` (severity none).
    ``escalate`` hands a step only a person may take to the member that files it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, model: RoutedModelCalling, escalations: Lazy[EscalationSink]) -> None:
        self.model, self.escalations = model, escalations

    def digest(self, **fields: Any) -> str:
        return overseer.build_digest(**fields)

    def oversee(self, provider: Any, model: str, digest: str) -> OverseerSignal:
        return overseer.oversee(provider, model, digest)

    def watch(self, digest: str, model: str = "") -> Any:
        return to_risk_signal(overseer.oversee(self.model, model, digest))

    def escalate(self, escalation: Escalation) -> str:
        return self.escalations.escalate(escalation)
