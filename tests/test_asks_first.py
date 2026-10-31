"""The planner's ``asks_first`` verdict: parsed as a boolean field, told to the reply writer as a directive, and stated in the rubric."""
from quest_ai_runner.core.orchestrator import OrchestratorConfig
from quest_ai_runner.core.orchestrator_decisions import normalize_decision
from quest_ai_runner.core.planner_prompt import ASK_FIRST_DIRECTIVE, PLANNER_PROFILES


def test_the_flag_is_read_from_the_decision_and_defaults_to_false():
    cfg = OrchestratorConfig()
    assert normalize_decision({"action": "answer", "asks_first": True}, cfg).asks_first is True
    assert normalize_decision({"action": "answer"}, cfg).asks_first is False
    assert normalize_decision({"action": "answer", "asks_first": "yes"}, cfg).asks_first is False


def test_the_rubric_asks_the_planner_to_set_it_and_the_directive_forbids_a_plan():
    assert all("`asks_first`" in text for text in PLANNER_PROFILES.values())
    assert "No plan" in ASK_FIRST_DIRECTIVE and "—" not in ASK_FIRST_DIRECTIVE
