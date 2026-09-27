"""QuestClient.list_teams -- GET /api/teams, the org-level view, never raises.

"Which quests can you act on" is answered by walking every team the account belongs to and
listing each team's quests, not by the one configured ``team_id``. This is the read that starts
that walk, reachable from the CLI as ``quest-ai-runner quest list_teams``.
"""
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_returning(response):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        return response

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_list_teams_gets_the_account_wide_teams_endpoint():
    teams = [{"team_id": "team_1", "name": "Product"}, {"team_id": "team_2", "name": "Growth"}]
    client, captured = client_returning(teams)
    assert client.list_teams() == teams
    assert captured == {"method": "GET", "path": "/api/teams"}


def test_list_teams_unwraps_a_teams_envelope():
    client, _ = client_returning({"teams": [{"team_id": "team_1"}]})
    assert client.list_teams() == [{"team_id": "team_1"}]


def test_list_teams_none_response_returns_empty_list():
    client, _ = client_returning(None)
    assert client.list_teams() == []


def test_list_teams_never_raises_on_api_error():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(*a, **k):
        raise QuestApiError("Quest API GET /api/teams -> 500: boom")

    client._request = failing_request  # type: ignore[assignment]
    assert client.list_teams() == []


def test_list_teams_is_a_read_for_the_quest_cli():
    from quest_ai_runner.cli import _quest_method_is_read_only

    assert _quest_method_is_read_only("list_teams")
