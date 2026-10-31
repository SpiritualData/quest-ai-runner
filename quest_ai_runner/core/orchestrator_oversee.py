"""The overseer: a mixin of ``Orchestrator`` (see ``orchestrator.py``) over the hub's ``hubofhubs.orchestrate.oversee``.

The hub holds the plumbing (spend metrics, digest call, fire-and-forget consult, collect, late follow-up). QAR supplies the four
hooks it reads: how an observation is summarized, the digest (QAR's overseer prompt and policy, ``core/overseer.py``), the
consult, and the late human escalation (a QAR ``Escalation`` raised through the wired sink). The old underscore method names
stay on the class for the run phases that call them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from hubofhubs.orchestrate.oversee import OverseeMixin as HubOverseeMixin
from .adapters_values import Escalation
from .overseer import OverseerSignal, build_digest, oversee  # noqa: F401
from .planner_context import _summarize_observation


class OverseeMixin(HubOverseeMixin):
    """The overseer: the hub's mixin with QAR's hooks."""

    _run_spend_metrics = HubOverseeMixin.spend_metrics
    _oversee_operation_lines = HubOverseeMixin.operation_lines
    _build_oversee_digest = HubOverseeMixin.build_oversee_digest
    _submit_oversee = HubOverseeMixin.submit_oversee
    _collect_oversee = HubOverseeMixin.collect_oversee

    def _finish_oversee_in_background(self, pending: Optional[Dict[str, Any]], *, emit: Any,
                                      quest_id: Optional[str] = None, brainstorm_active: bool = False) -> None:
        self.finish_oversee_in_background(pending, emit=emit, scope_id=quest_id, brainstorm_active=brainstorm_active)

    def oversee_summarize_observation(self, observation: Dict[str, Any]) -> str:
        return _summarize_observation(observation)

    def oversee_digest(self, **fields: Any) -> str:
        return build_digest(**fields)

    def oversee_consult(self, provider: Any, model: str, digest: str) -> OverseerSignal:
        return oversee(provider, model, digest)

    def oversee_escalate_late(self, reason: Optional[str], scope_id: Optional[str]) -> None:
        """A late ``escalate_human`` verdict becomes a real decision-request through the wired ``EscalationSink``."""
        q = reason or "A prior response may need your input; please confirm."
        self.escalation.escalate(Escalation(
            summary=self._concise_decision_summary(q),
            kind="approve", quest_id=scope_id, default_on_silence="hold"))
