"""QuestClient.create_task must enqueue with an API-accepted source and fail LOUDLY.

Two real-world failures found by live testing (2026-07): the default ``source="cli"`` was
rejected by the Quest API's enum (chat / reflection / review) with a 400, and create_task
swallowed that error into ``{}`` - so ``cli send`` acknowledged the user ("I'm looking into
it") for a task that was never enqueued and would never run. The exact silent-failure mode
the reliability work bans: an ack is a promise, so enqueue failures must raise.
"""
import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_body():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "task_1"}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_create_task_defaults_to_an_api_accepted_source():
    client, captured = client_capturing_body()
    client.create_task("do the thing")
    assert captured["path"] == "/api/assistant-tasks"
    # The API enum is chat / reflection / review; "cli" was rejected with a 400.
    assert captured["body"]["source"] == "chat"


def test_create_task_raises_instead_of_swallowing_api_errors():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError("Quest API POST /api/assistant-tasks -> 400: bad source")

    client._request = failing_request  # type: ignore[assignment]
    with pytest.raises(QuestApiError):
        client.create_task("do the thing")


def test_create_task_passes_routing_fields():
    client, captured = client_capturing_body()
    client.create_task("t", team_id="team_9", goal_id="goal_3", scheduled_at="2026-07-12T09:00:00Z")
    body = captured["body"]
    assert body["team_id"] == "team_9"
    assert body["goal_id"] == "goal_3"
    assert body["scheduled_at"] == "2026-07-12T09:00:00Z"


def test_create_task_includes_card_ids_when_given():
    client, captured = client_capturing_body()
    client.create_task("t", card_ids=["a", "b"])
    assert captured["body"]["card_ids"] == ["a", "b"]


def test_create_task_omits_card_ids_when_not_given():
    client, captured = client_capturing_body()
    client.create_task("t")
    assert "card_ids" not in captured["body"]


def test_create_task_omits_card_ids_when_empty_list():
    client, captured = client_capturing_body()
    client.create_task("t", card_ids=[])
    assert "card_ids" not in captured["body"]
