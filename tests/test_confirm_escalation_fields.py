"""The planner can specify a confirm/clarify decision's kind, deadline, and default_on_silence.

Before this, every confirm/clarify escalation hardcoded kind="approve" (or "clarify") and
default_on_silence="hold", with no way to ask for a deadline at all. The planner's decide-tool
schema now carries optional confirm_kind / confirm_deadline / confirm_default_on_silence, parsed
into PlanDecision and threaded onto the Escalation the orchestrator raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubEscalation, StubProvider, StubRetrieval


def make_orchestrator(provider, retrieval, **kw):
    cfg = kw.pop("config", None) or OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), config=cfg, **kw)


def test_confirm_honors_planner_kind_deadline_and_default_on_silence():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Spend $500 on ads?",
         "confirm_kind": "explicit:spend", "confirm_deadline": "in 48h",
         "confirm_default_on_silence": "proceed", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_spend")
    before = datetime.now(timezone.utc)
    res = make_orchestrator(provider, StubRetrieval(), escalation=sink).run(
        "spend $500 on ads", quest_id="quest_1")

    assert res.kind == "confirm"
    raised = sink.raised[0]
    assert raised.kind == "explicit:spend"
    assert raised.default_on_silence == "proceed"
    assert raised.deadline is not None
    assert before + timedelta(hours=47) < raised.deadline < before + timedelta(hours=49)


def test_confirm_defaults_are_unchanged_when_planner_omits_the_new_fields():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Buy item X for $50?", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_abc")
    res = make_orchestrator(provider, StubRetrieval(), escalation=sink).run(
        "buy item X", quest_id="quest_1")

    assert res.kind == "confirm"
    raised = sink.raised[0]
    assert raised.kind == "approve"
    assert raised.default_on_silence == "hold"
    assert raised.deadline is None


def test_clarify_honors_planner_kind_and_deadline():
    provider = StubProvider(decisions=[
        {"action": "clarify",
         "clarification": {"question": "Which campaign?", "options": ["A", "B"]},
         "confirm_kind": "notice:prod-ops", "confirm_deadline": "in 2 days",
         "rationale": "need a choice"},
    ])
    sink = StubEscalation(decision_id="dec_choice")
    before = datetime.now(timezone.utc)
    res = make_orchestrator(provider, StubRetrieval(), escalation=sink).run(
        "which campaign should run", quest_id="quest_1")

    assert res.kind == "confirm"
    raised = sink.raised[0]
    assert raised.kind == "notice:prod-ops"
    assert before + timedelta(hours=47) < raised.deadline < before + timedelta(hours=49)


def test_confirm_deadline_garbage_degrades_to_no_deadline():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Buy item X?",
         "confirm_deadline": "whenever, no rush", "rationale": "money"},
    ])
    sink = StubEscalation(decision_id="dec_abc")
    res = make_orchestrator(provider, StubRetrieval(), escalation=sink).run(
        "buy item X", quest_id="quest_1")
    assert sink.raised[0].deadline is None
