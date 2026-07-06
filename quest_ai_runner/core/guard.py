"""Execution record — the durable per-turn account of what actually executed.

The risk this manages (Joshua, 2026-06-15): the assistant SAYS it did something when the action
did not actually execute or finish. The brain's read-and-answer step can never change files, code,
data, or configuration itself; only deep runs can. So the orchestrator keeps an ``ExecutionRecord``
of every mutating action that ran this turn (built from ``DeepResult.met`` and any ``EVENT_EXEC``
lifecycle ticks), and the goal verification (``Orchestrator._verify_goal`` with ``verify_claims``
on) judges every answer against it: a reply claiming a completed change the record does not back
is remediated (executed for real when nothing ran, else rewritten to be honest and flagged
``partial`` so a background task maps to needs_you / failed, not done).

Claim DETECTION is deliberately not done here with regexes: the goal-verification LLM call reads
the reply and the record together and decides, so no phrasing can slip past a pattern list. (An
earlier version gated the check on a regex claim detector; it missed real phrasings, e.g. adverbs
between the auxiliary and the verb, and was removed in favor of always verifying.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecutionFact:
    """One mutating-action outcome observed this turn.

    ``goal`` is the deep goal/brief the action was run under (human-readable). ``succeeded`` /
    ``failed`` are derived from ``DeepResult.met`` and any ``EVENT_EXEC`` phase ticks. A fact with
    neither succeeded nor failed means the action was REQUESTED but produced no recorded outcome
    (e.g. no runner wired) — the one case where safe re-execution is possible.
    """
    goal: str = ""
    succeeded: bool = False
    failed: bool = False
    error: Optional[str] = None
    # Raw EVENT_EXEC phase ticks seen for this action (for tracing; not required for the decision).
    phases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"goal": self.goal, "succeeded": self.succeeded, "failed": self.failed}
        if self.error:
            d["error"] = self.error
        if self.phases:
            d["phases"] = self.phases
        return d


@dataclass
class ExecutionRecord:
    """The whole turn's execution facts, attached to ``OrchestratorResult.execution_record``."""
    facts: List[ExecutionFact] = field(default_factory=list)

    @property
    def any_mutation_attempted(self) -> bool:
        return bool(self.facts)

    @property
    def any_success(self) -> bool:
        return any(f.succeeded for f in self.facts)

    @property
    def any_failure(self) -> bool:
        return any(f.failed for f in self.facts)

    def summary(self) -> str:
        """A compact, human/LLM-readable rendering of what actually executed this turn."""
        if not self.facts:
            return "NO action/operation executed this turn (no mutating work ran)."
        lines: List[str] = []
        for f in self.facts:
            if f.succeeded:
                state = "SUCCEEDED"
            elif f.failed:
                state = "FAILED" + (f" ({f.error})" if f.error else "")
            else:
                state = "NO RECORDED OUTCOME (requested but neither success nor failure recorded)"
            lines.append(f"- action {f.goal!r}: {state}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {"facts": [f.to_dict() for f in self.facts]}


# Phases (consumer-defined on EVENT_EXEC ``data``) that signal a terminal outcome for an action.
_SUCCESS_PHASES = {"done", "succeeded", "success", "complete", "completed"}
_FAILURE_PHASES = {"error", "failed", "failure"}
# A retry tick on its own is in-flight, not terminal; only an EXHAUSTED retry would be a failure,
# which the deep runner reports via DeepResult.met=False (the authoritative signal we already use).


def classify_exec_phase(phase: Optional[str]) -> Optional[str]:
    """Map an EVENT_EXEC phase string to "success" | "failure" | None (non-terminal). Never raises."""
    if not isinstance(phase, str):
        return None
    p = phase.strip().lower()
    if p in _SUCCESS_PHASES:
        return "success"
    if p in _FAILURE_PHASES:
        return "failure"
    return None
