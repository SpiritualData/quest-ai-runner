"""Env vars newly wired into ``cli._config_from_env`` reach the ``RunnerConfig`` field they name.

Before this, ``discovery_team_id``, ``deep_max_turns`` (OrchestratorConfig), ``autopilot_pass_time``,
``autopilot_adopt_recurring``, ``rep_sync_direction``, and ``default_assignee_user_id`` were real
fields with no env-var path to them at all, forcing any consumer that needed one to write a `.py`
consumer instead of using the stock CLI. ``context_preamble`` did not exist as a field before this
change. Each test here proves the matching env var actually lands on the field, following the same
"one env var per test" style as the rest of this suite (see ``test_cli_create_goal.py``).
"""
from __future__ import annotations

import pytest

from quest_ai_runner import cli


def _base_env(monkeypatch):
    monkeypatch.setenv("QUEST_BASE_URL", "https://quest.example")
    monkeypatch.setenv("QUEST_API_KEY", "qsk_test")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_1")


# --- discovery_team_id -------------------------------------------------------

def test_discovery_team_id_unset_stays_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_DISCOVERY_TEAM_ID", raising=False)
    cfg = cli._config_from_env()
    assert cfg.discovery_team_id is None


def test_discovery_team_id_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_DISCOVERY_TEAM_ID", "team_other")
    cfg = cli._config_from_env()
    assert cfg.discovery_team_id == "team_other"


def test_discovery_team_id_explicit_empty_string_means_owner_scoped(monkeypatch):
    """"" is a real, distinct value (owner-scoped discovery) — must not collapse to None."""
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_DISCOVERY_TEAM_ID", "")
    cfg = cli._config_from_env()
    assert cfg.discovery_team_id == ""


# --- default_assignee_user_id (QAR_DECISION_ASSIGNEE) -----------------------

def test_decision_assignee_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_DECISION_ASSIGNEE", "user_42")
    cfg = cli._config_from_env()
    assert cfg.default_assignee_user_id == "user_42"


def test_decision_assignee_unset_stays_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_DECISION_ASSIGNEE", raising=False)
    cfg = cli._config_from_env()
    assert cfg.default_assignee_user_id is None


# --- deep_max_turns (OrchestratorConfig, not a RunnerConfig field directly) -

def test_deep_max_turns_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_DEEP_MAX_TURNS", "60")
    cfg = cli._config_from_env()
    assert cfg.orchestrator.deep_max_turns == 60


def test_deep_max_turns_invalid_value_is_ignored_not_raised(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_DEEP_MAX_TURNS", "not-a-number")
    cfg = cli._config_from_env()  # must not raise
    from quest_ai_runner.core.orchestrator import DEFAULT_DEEP_MAX_TURNS
    assert cfg.orchestrator.deep_max_turns == DEFAULT_DEEP_MAX_TURNS


# --- rep_sync_direction ------------------------------------------------------

def test_rep_sync_direction_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_REP_SYNC_DIRECTION", "both")
    cfg = cli._config_from_env()
    assert cfg.rep_sync_direction == "both"


def test_rep_sync_direction_default_unchanged(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_REP_SYNC_DIRECTION", raising=False)
    cfg = cli._config_from_env()
    assert cfg.rep_sync_direction == "pull"


# --- autopilot_pass_time / autopilot_adopt_recurring -------------------------

def test_autopilot_pass_time_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_AUTOPILOT_PASS_TIME", "06:00")
    cfg = cli._config_from_env()
    assert cfg.autopilot_pass_time == "06:00"


def test_autopilot_adopt_recurring_true(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_AUTOPILOT_ADOPT_RECURRING", "true")
    cfg = cli._config_from_env()
    assert cfg.autopilot_adopt_recurring is True


def test_autopilot_adopt_recurring_false(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_AUTOPILOT_ADOPT_RECURRING", "0")
    cfg = cli._config_from_env()
    assert cfg.autopilot_adopt_recurring is False


def test_autopilot_adopt_recurring_unset_stays_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_AUTOPILOT_ADOPT_RECURRING", raising=False)
    cfg = cli._config_from_env()
    assert cfg.autopilot_adopt_recurring is None


# --- context_preamble (inline + file) ---------------------------------------

def test_context_preamble_inline_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("QAR_CONTEXT_PREAMBLE_FILE", raising=False)
    monkeypatch.setenv("QAR_CONTEXT_PREAMBLE", "Act as the team's assistant.")
    cfg = cli._config_from_env()
    assert cfg.context_preamble == "Act as the team's assistant."


def test_context_preamble_file_wins_over_inline(monkeypatch, tmp_path):
    _base_env(monkeypatch)
    preamble_file = tmp_path / "preamble.txt"
    preamble_file.write_text("Multi-line org doctrine.\nSecond line.\n")
    monkeypatch.setenv("QAR_CONTEXT_PREAMBLE_FILE", str(preamble_file))
    monkeypatch.setenv("QAR_CONTEXT_PREAMBLE", "should be overridden")
    cfg = cli._config_from_env()
    assert cfg.context_preamble == "Multi-line org doctrine.\nSecond line.\n"


def test_context_preamble_file_unreadable_logs_and_leaves_preamble_unset(monkeypatch, tmp_path, caplog):
    import logging
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_CONTEXT_PREAMBLE_FILE", str(tmp_path / "does_not_exist.txt"))
    monkeypatch.delenv("QAR_CONTEXT_PREAMBLE", raising=False)
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner"):
        cfg = cli._config_from_env()
    assert cfg.context_preamble is None
    assert any("QAR_CONTEXT_PREAMBLE_FILE" in r.getMessage() for r in caplog.records)


def test_context_preamble_reaches_the_auto_built_deep_runner(monkeypatch, tmp_path):
    """The whole point of the field: no SubprocessConfig written by hand to set a preamble."""
    from quest_ai_runner.config import resolve_deep_runner

    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("QAR_CLAUDE_PATH", str(binary))
    _base_env(monkeypatch)
    monkeypatch.setenv("QAR_CONTEXT_PREAMBLE", "Ground on the corpus.")
    cfg = cli._config_from_env()
    runner = resolve_deep_runner(cfg)
    assert runner is not None
    assert runner.cfg.context_preamble == "Ground on the corpus."
