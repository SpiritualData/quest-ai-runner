"""Orphan-decision recovery: a deep run that raised a decision but printed no marker.

The QAR-ESCALATED marker is the worker's own text-based report of a decision it created directly
against the consumer's API (its own credentials, not this process's escalation sink). If that text
is missing entirely, the run's DeepResult carries no decision_id and used to close the task done,
leaving the decision it actually created orphaned -- open forever, linked to nothing.

The Orchestrator now diffs a before/after snapshot of the quest's open decisions (via the OPTIONAL
``EscalationSink.open_decision_ids_for_quest`` capability) around each deep attempt, and recovers
the new id when the marker path found nothing.
"""
from __future__ import annotations

from typing import Any, FrozenSet, List

from quest_ai_runner.core.adapters import Escalation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


def make_orchestrator(provider, retrieval, **kw):
    cfg = kw.pop("config", None) or OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), config=cfg, **kw)


class SequencedSnapshotEscalation:
    """An EscalationSink whose ``open_decision_ids_for_quest`` returns a different snapshot on
    each successive call, simulating a decision appearing on the quest DURING a deep run that
    never went through THIS sink instance (the worker created it directly)."""

    def __init__(self, snapshots: List[FrozenSet[str]]):
        self.snapshots = list(snapshots)
        self.calls_made = 0
        self.raised: List[Escalation] = []

    def escalate(self, escalation: Escalation) -> str:
        self.raised.append(escalation)
        return ""

    def open_decision_ids_for_quest(self, quest_id: str) -> FrozenSet[str]:
        idx = min(self.calls_made, len(self.snapshots) - 1)
        self.calls_made += 1
        return self.snapshots[idx]


class NoRecoverySink:
    """An EscalationSink implementing only the required ``escalate`` -- the ordinary case, with
    no recovery capability at all. Must never be called into for the probe (no crash either)."""

    def escalate(self, escalation: Escalation) -> str:
        return ""


def test_recovers_decision_when_marker_missing_but_new_decision_appeared():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Send the campaign", "deep_brief": "do it",
         "rationale": "real work"},
    ])
    # The worker's output looks like an ordinary success: met=True, no decision_id (no marker).
    runner = StubDeepRunner(met=True, output="sent the campaign")
    sink = SequencedSnapshotEscalation([frozenset(), frozenset({"dec_orphan"})])
    res = make_orchestrator(provider, StubRetrieval(), deep_runner=runner, escalation=sink).run(
        "send the campaign", quest_id="quest_1")

    assert res.kind == "deep"
    assert len(res.deep_results) == 1
    recovered = res.deep_results[0]
    assert recovered.decision_id == "dec_orphan"
    assert recovered.met is False, "a recovered decision must pause the run, never report met"


def test_no_recovery_when_no_new_decision_appeared():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Send the campaign", "deep_brief": "do it",
         "rationale": "real work"},
        {"met": True, "reason": "done"},  # goal verification
    ])
    runner = StubDeepRunner(met=True, output="sent the campaign")
    sink = SequencedSnapshotEscalation([frozenset({"dec_x"}), frozenset({"dec_x"})])
    res = make_orchestrator(provider, StubRetrieval(), deep_runner=runner, escalation=sink).run(
        "send the campaign", quest_id="quest_1")

    assert res.kind == "deep"
    assert res.deep_results[0].decision_id is None
    assert res.deep_results[0].met is True


def test_probe_is_a_no_op_for_a_sink_without_the_capability():
    """An EscalationSink implementing only ``escalate`` (the ordinary/required surface) must not
    break a deep run just because it lacks the optional recovery method."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Send the campaign", "deep_brief": "do it",
         "rationale": "real work"},
        {"met": True, "reason": "done"},
    ])
    runner = StubDeepRunner(met=True, output="sent the campaign")
    res = make_orchestrator(provider, StubRetrieval(), deep_runner=runner,
                            escalation=NoRecoverySink()).run("send the campaign", quest_id="quest_1")
    assert res.kind == "deep"
    assert res.deep_results[0].decision_id is None
    assert res.deep_results[0].met is True


def test_marker_based_decision_id_is_unaffected_by_the_probe():
    """When the worker DID report via the marker (decision_id already set), the probe must not
    run at all and must not override it."""
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Send the campaign", "deep_brief": "do it",
         "rationale": "real work"},
    ])
    runner = StubDeepRunner(met=False, output="need approval", decision_id="dec_marker")
    sink = SequencedSnapshotEscalation([frozenset(), frozenset({"dec_other"})])
    res = make_orchestrator(provider, StubRetrieval(), deep_runner=runner, escalation=sink).run(
        "send the campaign", quest_id="quest_1")
    assert res.deep_results[0].decision_id == "dec_marker"
