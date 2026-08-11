"""Offline tests for the `create-goal` CLI subcommand's argv wiring.

Proves the CLI layer passes the right fields through to QuestClient.create_goal (title, optional
quest-id, period defaulting to today when omitted) and never acks a goal the API didn't actually
return an id for -- same contract `send` holds for tasks.
"""
from __future__ import annotations

from datetime import date

import pytest

from quest_ai_runner import cli
from quest_ai_runner.runner.quest_client import QuestApiError


def _patch_env(monkeypatch):
    monkeypatch.setenv("QUEST_BASE_URL", "https://quest.example")
    monkeypatch.setenv("QUEST_API_KEY", "test-api-key")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_1")


def test_create_goal_passes_title_quest_id_and_period(monkeypatch, capsys):
    _patch_env(monkeypatch)
    captured = {}

    def fake_create_goal(self, title, **kwargs):
        captured["title"] = title
        captured.update(kwargs)
        return {"id": "goal_1", "deadline": "2026-08-18"}

    monkeypatch.setattr(
        "quest_ai_runner.runner.quest_client.QuestClient.create_goal", fake_create_goal)

    rc = cli.main(["create-goal", "Fill out the form",
                   "--quest-id", "quest_9", "--period", "2026-08-18"])

    assert rc == 0
    assert captured["title"] == "Fill out the form"
    assert captured["quest_id"] == "quest_9"
    assert captured["period"] == "2026-08-18"
    out = capsys.readouterr().out
    assert "goal_1" in out


def test_create_goal_defaults_period_to_today(monkeypatch):
    _patch_env(monkeypatch)
    captured = {}

    def fake_create_goal(self, title, **kwargs):
        captured.update(kwargs)
        return {"id": "goal_1", "deadline": kwargs["period"]}

    monkeypatch.setattr(
        "quest_ai_runner.runner.quest_client.QuestClient.create_goal", fake_create_goal)

    rc = cli.main(["create-goal", "No period given"])

    assert rc == 0
    assert captured["period"] == date.today().isoformat()


def test_create_goal_quest_id_omitted_when_not_given(monkeypatch):
    _patch_env(monkeypatch)
    captured = {}

    def fake_create_goal(self, title, **kwargs):
        captured.update(kwargs)
        return {"id": "goal_1", "deadline": kwargs["period"]}

    monkeypatch.setattr(
        "quest_ai_runner.runner.quest_client.QuestClient.create_goal", fake_create_goal)

    rc = cli.main(["create-goal", "Standalone goal"])

    assert rc == 0
    assert captured["quest_id"] is None


def test_create_goal_reports_api_failure_and_returns_nonzero(monkeypatch, capsys):
    _patch_env(monkeypatch)

    def failing_create_goal(self, title, **kwargs):
        raise QuestApiError("Quest API POST /api/planning/goals -> 400: bad period")

    monkeypatch.setattr(
        "quest_ai_runner.runner.quest_client.QuestClient.create_goal", failing_create_goal)

    rc = cli.main(["create-goal", "Doomed goal"])

    assert rc == 1
    assert "Could not create the goal" in capsys.readouterr().out


def test_create_goal_missing_id_in_response_is_treated_as_failure(monkeypatch, capsys):
    _patch_env(monkeypatch)

    def fake_create_goal(self, title, **kwargs):
        return {}  # API answered but with no goal id

    monkeypatch.setattr(
        "quest_ai_runner.runner.quest_client.QuestClient.create_goal", fake_create_goal)

    rc = cli.main(["create-goal", "No id returned"])

    assert rc == 1
    assert "no goal id" in capsys.readouterr().out.lower()


def test_create_goal_requires_base_url_and_api_key(monkeypatch, capsys):
    monkeypatch.delenv("QUEST_BASE_URL", raising=False)
    monkeypatch.delenv("QUEST_API_KEY", raising=False)

    rc = cli.main(["create-goal", "Missing config"])

    assert rc == 1
