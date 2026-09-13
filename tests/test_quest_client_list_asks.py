"""QuestClient.list_asks -- feeds Autopilot's REACTIVE-mode gate (see has_new_ask_since /
_gate_quest in runner/autopilot.py): whether a quest has anything new to react to since its last
pass. Must never raise -- a transient read failure here must never silently wedge a reactive
quest as "always due" (it degrades to an empty list, which reads as "nothing new").
"""
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_call():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return {"items": [{"key": "a1", "occurred_at": "2026-07-05T00:00:00Z"}], "total": 1}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_list_asks_queries_the_quest_scoped_to_every_author():
    client, captured = client_capturing_call()
    result = client.list_asks(quest_id="quest_1")
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/asks"
    # mine_only must be explicitly false: the gate needs every ask on the quest, not only ones
    # the calling (autopilot) account itself filed.
    assert captured["params"]["quest_id"] == "quest_1"
    assert captured["params"]["mine_only"] == "false"
    assert result == [{"key": "a1", "occurred_at": "2026-07-05T00:00:00Z"}]


def test_list_asks_passes_a_limit():
    client, captured = client_capturing_call()
    client.list_asks(quest_id="quest_1", limit=10)
    assert captured["params"]["limit"] == 10


def test_list_asks_degrades_to_empty_list_on_api_error():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError("Quest API GET /api/asks -> 500")

    client._request = failing_request  # type: ignore[assignment]
    assert client.list_asks(quest_id="quest_1") == []


def test_list_asks_tolerates_a_malformed_response_shape():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    client._request = lambda *a, **k: {"items": "not-a-list"}  # type: ignore[assignment]
    assert client.list_asks(quest_id="quest_1") == []

    client._request = lambda *a, **k: None  # type: ignore[assignment]
    assert client.list_asks(quest_id="quest_1") == []
