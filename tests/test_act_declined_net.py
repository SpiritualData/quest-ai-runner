"""An answer that declines an act on the records it read is the whole response (chat_quality IMP-028).

"Can you help me book Ana for Friday?" is an order (user_intent "act"). The planner read the quest
note saying Ana teaches Tuesday and Thursday evenings, and answered that Friday will not work. The
decisive fallback then saw "act, nothing mutated" and queued a background task, discarding that
answer. The planner's own structured verdict `act_declined` now says the answer is the complete
response, and the nets that escalate an "act" answer to deep honor it. Also pins the three prompt
rules (the planner rubric, the overseer, the queued-hand-off sentence). Offline, no network.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.core.orchestrator_decisions import normalize_decision
from quest_ai_runner.core.overseer import OVERSEER_PROMPT
from quest_ai_runner.core.planner_prompt import (
    DEFERRED_DEEP_QUEUED_SEMANTICS,
    PLANNER_DECISION_RUBRIC,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

EM_DASH = "—"
MESSAGE = "can you help me book the tutor for Friday"


def run_turn(decision, answer):
    provider = StubProvider(decisions=[decision], answer_text=answer)
    runner = StubDeepRunner(met=True, output="booked")
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner)
    return orch.run(MESSAGE), runner


def test_a_declined_act_stays_the_answer_and_opens_no_deep_run():
    res, runner = run_turn(
        {"action": "answer", "model_tier": "sonnet", "rationale": "the note rules Friday out",
         "user_intent": "act", "act_declined": True},
        "Friday will not work: your note says the tutor only teaches Tuesday and Thursday evenings.")
    assert res.kind == "answer"
    assert "Tuesday" in res.text
    assert not runner.calls, "the declined act is answered, not queued or executed"


def test_the_same_act_answer_without_the_verdict_still_escalates():
    res, runner = run_turn(
        {"action": "answer", "model_tier": "sonnet", "rationale": "propose", "user_intent": "act"},
        "I could book the tutor for Friday.")
    assert runner.calls, "no act_declined verdict keeps the existing escalation"


def test_act_declined_is_true_only_for_a_real_boolean():
    from quest_ai_runner.core.adapters_values import PlanDecision  # noqa: F401
    cfg = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider(decisions=[]),
                       registry=ModelRegistry(StubProvider(decisions=[]))).cfg
    assert normalize_decision({"action": "answer", "act_declined": True}, cfg).act_declined is True
    assert normalize_decision({"action": "answer", "act_declined": "true"}, cfg).act_declined is False
    assert normalize_decision({"action": "answer"}, cfg).act_declined is False


def test_the_planner_rubric_states_answer_first_and_the_declined_verdict():
    assert "ANSWER WHAT YOU ALREADY HOLD" in PLANNER_DECISION_RUBRIC
    assert "`act_declined`" in PLANNER_DECISION_RUBRIC
    assert "what you could not check" in PLANNER_DECISION_RUBRIC
    assert EM_DASH not in PLANNER_DECISION_RUBRIC


def test_the_overseer_does_not_escalate_over_records_that_decide_the_act():
    assert "RECORDS IN HAND COME FIRST" in OVERSEER_PROMPT
    assert "never escalate_deep while the stored facts decide" in OVERSEER_PROMPT
    assert EM_DASH not in OVERSEER_PROMPT


def test_the_queued_handoff_is_for_long_or_outside_work_only():
    assert "long-running" in DEFERRED_DEEP_QUEUED_SEMANTICS
    assert "runs now" in DEFERRED_DEEP_QUEUED_SEMANTICS
