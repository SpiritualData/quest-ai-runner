"""QuestClient.post_environment_heartbeat must route to org vs team scope by org_id.

An execution environment is how a quest-ai-runner instance tells the Quest backend "I'm alive,
here's what I can do" so the backend can route deferred AI work to it. Team-scoped heartbeats
(POST /api/teams/{team_id}/environment/heartbeat) register the runner for one team; org-scoped
heartbeats (POST /api/orgs/{org_id}/environment/heartbeat) register it for EVERY team in that org
-- the mechanism a shared/org-wide runner deployment needs. This file locks down that routing
choice and guards the pre-existing team-scoped behavior against regression.
"""
import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestNotConfigured, QuestClient


def client_capturing_body(team_id="team_1"):
    client = QuestClient("https://quest.example", "test-api-key", team_id=team_id)
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"ok": True}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_heartbeat_with_org_id_posts_to_org_scoped_endpoint():
    client, captured = client_capturing_body(team_id="team_1")
    client.post_environment_heartbeat({"web": True, "corpus": True, "code": True},
                                       org_id="org_example")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/orgs/org_example/environment/heartbeat"
    assert captured["body"] == {"capabilities": {"web": True, "corpus": True, "code": True}}


def test_heartbeat_with_org_id_ignores_team_id_requirement():
    # No team_id configured at all -- org-scope registration doesn't need one.
    client, captured = client_capturing_body(team_id="")
    result = client.post_environment_heartbeat({"web": False, "corpus": True, "code": False},
                                                 org_id="org_example")
    assert captured["path"] == "/api/orgs/org_example/environment/heartbeat"
    assert result == {"ok": True}


def test_heartbeat_with_org_id_includes_runner_label_and_env_id():
    client, captured = client_capturing_body(team_id="team_1")
    client.post_environment_heartbeat({"web": True, "corpus": True, "code": True},
                                       org_id="org_example", runner_label="shared-runner",
                                       env_id="env_2")
    assert captured["path"] == "/api/orgs/org_example/environment/heartbeat"
    assert captured["body"]["runner_label"] == "shared-runner"
    assert captured["body"]["env_id"] == "env_2"


def test_heartbeat_with_no_org_id_keeps_hitting_team_url_exactly_as_before():
    """Regression guard: omitting org_id must not change the pre-existing team-scoped behavior."""
    client, captured = client_capturing_body(team_id="team_1")
    client.post_environment_heartbeat({"web": True, "corpus": True, "code": True},
                                       runner_label="my-runner", env_id="env_1")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/teams/team_1/environment/heartbeat"
    assert captured["body"] == {
        "capabilities": {"web": True, "corpus": True, "code": True},
        "runner_label": "my-runner",
        "env_id": "env_1",
    }


def test_heartbeat_with_no_org_id_and_no_team_id_returns_empty_and_logs_not_raises():
    """Without org_id, the pre-existing team_id requirement still applies -- but the documented
    "never raises" contract holds: a missing team_id is caught and swallowed to {}."""
    client, captured = client_capturing_body(team_id="")
    result = client.post_environment_heartbeat({"web": True, "corpus": True, "code": True})
    assert result == {}
    assert "path" not in captured  # _request was never reached


def test_heartbeat_org_id_takes_precedence_over_explicit_team_id():
    client, captured = client_capturing_body(team_id="team_1")
    client.post_environment_heartbeat({"web": True, "corpus": True, "code": True},
                                       team_id="team_9", org_id="org_example")
    assert captured["path"] == "/api/orgs/org_example/environment/heartbeat"
