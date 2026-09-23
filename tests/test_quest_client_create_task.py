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


def test_create_task_sends_assignee_user_id_when_given():
    """``assignee_user_id`` sets the task's EXECUTOR (its owner), which is what task discovery
    scopes on. Without it the backend picks the linked quest's owner, so an app account's work on
    a human-owned quest is created into a lane that will never look for it."""
    client, captured = client_capturing_body()
    client.create_task("t", goal_id="quest_1", assignee_user_id="acct_app")
    assert captured["body"]["assignee_user_id"] == "acct_app"


def test_create_task_omits_assignee_user_id_when_not_given():
    """The field must be absent, not null: omitted, the backend keeps its own default executor."""
    client, captured = client_capturing_body()
    client.create_task("t")
    assert "assignee_user_id" not in captured["body"]


def test_create_task_sends_model_and_deep_run_model_as_independent_fields():
    """``model`` (a QAR-call tier) and ``deep_run_model`` (a literal deep-run model pin) are
    separate fields on the backend's create-task schema (split 2026-09-22) -- a caller holding
    both must be able to send both in the same call, and each lands under its own key."""
    client, captured = client_capturing_body()
    client.create_task("t", model="balanced", deep_run_model="opus")
    assert captured["body"]["model"] == "balanced"
    assert captured["body"]["deep_run_model"] == "opus"


def test_create_task_omits_deep_run_model_when_not_given():
    client, captured = client_capturing_body()
    client.create_task("t", model="balanced")
    assert "deep_run_model" not in captured["body"]
