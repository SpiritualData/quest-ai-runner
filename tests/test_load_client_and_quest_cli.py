"""``load_client`` / ``load_quest_client``, decision-assignee routing, and the ``quest`` CLI
subcommand.

Covers the lightweight scripting front door added alongside the full ``load_config`` +
``run_lane`` path: a script or a shell one-liner that only wants a ``QuestClient`` (no adapter
stack, no optional deps) via ``quest_ai_runner.load_client`` / config-file ``[decision_assignees]``
/ ``QAR_DECISION_ASSIGNEES`` / ``QuestClient.assignee_id`` / ``create_decision(assignee=...)`` /
``quest-ai-runner quest <method>``. All offline: ``QuestClient._request`` is stubbed on the
instance wherever a body would otherwise go over the network, following the house style in
``tests/test_quest_client_create_task.py``.
"""
from __future__ import annotations

import textwrap

import pytest

from quest_ai_runner import cli
from quest_ai_runner.config import (
    ConfigFileError,
    RunnerConfig,
    load_quest_client,
    parse_decision_assignees,
)
from quest_ai_runner.runner.quest_client import QuestClient, QuestNotConfigured


def _write(tmp_path, text: str, name: str = "qar.toml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


def _clear_quest_env(monkeypatch):
    """Clear every env var these functions read, so a test never inherits the developer's own
    Quest deployment from their shell."""
    for var in ("QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID", "QAR_CONFIG_FILE",
                "QAR_DECISION_ASSIGNEES", "QAR_DECISION_ASSIGNEE"):
        monkeypatch.delenv(var, raising=False)


# --- parse_decision_assignees -------------------------------------------------

def test_parse_decision_assignees_json_object_form():
    assert parse_decision_assignees('{"owner": "user_abc", "operator": "user_def"}') == {
        "owner": "user_abc", "operator": "user_def"}


def test_parse_decision_assignees_name_equals_id_pairs():
    assert parse_decision_assignees("owner=user_abc,operator=user_def") == {
        "owner": "user_abc", "operator": "user_def"}


def test_parse_decision_assignees_single_pair_no_comma():
    assert parse_decision_assignees("owner=user_abc") == {"owner": "user_abc"}


@pytest.mark.parametrize("raw", [None, ""])
def test_parse_decision_assignees_empty_or_none_is_empty_dict(raw):
    assert parse_decision_assignees(raw) == {}


def test_parse_decision_assignees_malformed_json_raises():
    with pytest.raises(ConfigFileError, match="not valid JSON"):
        parse_decision_assignees('{"owner": not-json}')


def test_parse_decision_assignees_malformed_pair_raises():
    with pytest.raises(ConfigFileError, match="not 'name=user_id'"):
        parse_decision_assignees("owner-missing-equals-sign")


# --- QuestClient.assignee_id --------------------------------------------------

def _client_with_roles(**kwargs):
    return QuestClient(
        "https://quest.example", "test-api-key", team_id="team_1",
        decision_assignees={"owner": "user_owner", "operator": "user_operator"},
        default_assignee_user_id="user_default",
        **kwargs,
    )


def test_assignee_id_resolves_a_configured_role():
    client = _client_with_roles()
    assert client.assignee_id("owner") == "user_owner"
    assert client.assignee_id("operator") == "user_operator"


def test_assignee_id_none_returns_the_default():
    client = _client_with_roles()
    assert client.assignee_id(None) == "user_default"
    assert client.assignee_id() == "user_default"


def test_assignee_id_unknown_role_raises():
    client = _client_with_roles()
    with pytest.raises(QuestNotConfigured, match="unknown decision assignee"):
        client.assignee_id("nonexistent-role")


# --- create_decision(assignee=...) -------------------------------------------

def _client_capturing_body(**kwargs):
    client = _client_with_roles(**kwargs)
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "decision_1"}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_create_decision_assignee_role_resolves_to_assigned_to_user_id():
    client, captured = _client_capturing_body()
    client.create_decision("Approve the order", assignee="owner")
    assert captured["body"]["assigned_to_user_id"] == "user_owner"


def test_create_decision_explicit_assignee_user_id_wins_over_role():
    client, captured = _client_capturing_body()
    client.create_decision("Approve the order", assignee="owner",
                           assignee_user_id="user_explicit_override")
    assert captured["body"]["assigned_to_user_id"] == "user_explicit_override"


def test_create_decision_unknown_role_raises_rather_than_dropping_assignment():
    client, captured = _client_capturing_body()
    with pytest.raises(QuestNotConfigured):
        client.create_decision("Approve the order", assignee="nonexistent-role")
    assert "body" not in captured  # the request must never go out with the wrong/no assignee


# --- RunnerConfig.from_file: decision_assignees round-trip -------------------

def test_from_file_decision_assignees_round_trips(tmp_path):
    path = _write(tmp_path, """
        quest_base_url = "https://quest.example"
        team_id = "team_1"

        [decision_assignees]
        owner = "user_owner"
        operator = "user_operator"
    """)
    cfg = RunnerConfig.from_file(path)
    assert cfg.decision_assignees == {"owner": "user_owner", "operator": "user_operator"}


def test_from_file_decision_assignees_defaults_to_empty_dict(tmp_path):
    path = _write(tmp_path, 'quest_base_url = "https://quest.example"\n')
    cfg = RunnerConfig.from_file(path)
    assert cfg.decision_assignees == {}


# --- load_quest_client ---------------------------------------------------------

def test_load_quest_client_from_file_builds_a_configured_client(tmp_path, monkeypatch):
    _clear_quest_env(monkeypatch)
    path = _write(tmp_path, """
        quest_base_url = "https://quest.example"
        quest_api_key = "qsk_test_key"
        team_id = "team_1"

        [decision_assignees]
        owner = "user_owner"
    """)
    client = load_quest_client(str(path))
    assert isinstance(client, QuestClient)
    assert client.base_url == "https://quest.example"
    assert client.api_key == "qsk_test_key"
    assert client.team_id == "team_1"
    assert client.decision_assignees == {"owner": "user_owner"}


def test_load_quest_client_raises_when_unconfigured(monkeypatch):
    _clear_quest_env(monkeypatch)
    with pytest.raises(QuestNotConfigured):
        load_quest_client()


# --- CLI: quest subcommand read/write gate ------------------------------------

def test_quest_method_is_read_only_classifies_read_methods():
    assert cli._quest_method_is_read_only("whoami") is True
    assert cli._quest_method_is_read_only("assignee_id") is True
    assert cli._quest_method_is_read_only("get_task") is True
    assert cli._quest_method_is_read_only("list_quests") is True
    assert cli._quest_method_is_read_only("is_configured") is True
    assert cli._quest_method_is_read_only("search_cards") is True
    assert cli._quest_method_is_read_only("discover_due") is True
    assert cli._quest_method_is_read_only("owning_team_for") is True
    assert cli._quest_method_is_read_only("goals_from_quest") is True


def test_quest_method_is_read_only_classifies_write_methods():
    assert cli._quest_method_is_read_only("create_decision") is False
    assert cli._quest_method_is_read_only("delete_goal") is False
    assert cli._quest_method_is_read_only("create_task") is False


def test_main_quest_write_method_without_write_flag_returns_2(capsys):
    rc = cli.main(["quest", "delete_goal", "g1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--write" in err
