"""Core's tunables come from CoreSettings, resolved by the config layer (structure rule S13)."""
from quest_ai_runner.config_core_settings import core_settings_from_env
from quest_ai_runner.core.orchestrator_capabilities import read_op_timeout_seconds, verify_context_max_chars
from quest_ai_runner.core.runtime_settings import (
    CoreSettings, core_settings, install_core_settings, process_environment)


def test_defaults_match_the_old_environment_defaults():
    s = core_settings_from_env({})
    assert s == CoreSettings()
    assert (s.read_op_timeout_seconds, s.context_assembly_timeout_seconds, s.verify_context_max_chars) == (60.0, 30.0, 24000)
    assert s.deep_review_interval_seconds == 300.0 and s.deep_timeout_seconds is None


def test_bad_and_non_positive_values_fall_back():
    s = core_settings_from_env({"QAR_READ_OP_TIMEOUT_SECONDS": "oops", "QAR_QUEST_JUDGE_TIMEOUT_SECONDS": "-3",
                                "QAR_VERIFY_CONTEXT_MAX_CHARS": "0", "QAR_DEEP_PROMPT_TOKEN_BUDGET": "x",
                                "QAR_DEEP_REVIEW_INTERVAL_SECONDS": "0", "QAR_NOTE_FLOOR_RELEVANCE_GATED": "off"})
    assert s.read_op_timeout_seconds == 60.0 and s.quest_judge_timeout_seconds == 8.0
    assert s.verify_context_max_chars == 24000 and s.deep_prompt_token_budget is None
    assert s.deep_review_interval_seconds is None and s.note_floor_relevance_gated is False


def test_core_reads_the_environment_fresh_through_the_config_layer(monkeypatch):
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "12.5")
    assert read_op_timeout_seconds() == 12.5
    monkeypatch.setenv("QAR_VERIFY_CONTEXT_MAX_CHARS", "500")
    assert verify_context_max_chars() == 500
    monkeypatch.setenv("QAR_PROBE_VAR", "1")
    assert process_environment()["QAR_PROBE_VAR"] == "1"


def test_a_pinned_core_settings_wins_over_the_environment(monkeypatch):
    from quest_ai_runner.config_core_settings import core_settings_from_env as provider, install_for_config
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "99")
    try:
        install_for_config(CoreSettings(read_op_timeout_seconds=7.0))
        assert read_op_timeout_seconds() == 7.0
    finally:
        install_core_settings(provider)
    assert core_settings().read_op_timeout_seconds == 99.0
