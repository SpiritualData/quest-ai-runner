"""QuestClient.claim_task_messages -- POST .../messages/claim, ATOMIC claim, never raises.

The backend hands back every message on a task with ``delivered_at == None`` and stamps
``delivered_at`` on them in the SAME call, so a message is delivered to exactly one caller
exactly once (a re-poll right after returns ``[]`` for those same messages). This client method
is the thin, best-effort wrapper the executor's throttled ``pending_inputs`` poll relies on, so it
must degrade the same way ``report_progress`` / ``list_goal_notes`` do: never raise, safe-default
empty list.
"""
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_request(response):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return response

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_claim_task_messages_posts_to_the_claim_endpoint():
    client, captured = client_capturing_request(
        {"messages": [{"message_id": "amsg_1", "text": "hello",
                      "author_user_id": "u1", "at": "2026-07-24T00:00:00Z",
                      "delivered_at": "2026-07-24T00:00:01Z"}]})
    msgs = client.claim_task_messages("task_1")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/assistant-tasks/task_1/messages/claim"
    assert captured["body"] is None  # nothing to send; the task id rides in the URL
    assert msgs == [{"message_id": "amsg_1", "text": "hello", "author_user_id": "u1",
                     "at": "2026-07-24T00:00:00Z", "delivered_at": "2026-07-24T00:00:01Z"}]


def test_claim_task_messages_empty_case_returns_empty_list():
    client, _captured = client_capturing_request({"messages": []})
    assert client.claim_task_messages("task_1") == []


def test_claim_task_messages_none_response_returns_empty_list():
    """A bare-None body (e.g. an empty 200) must degrade to [], not raise on ``.get``."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    client._request = lambda *a, **k: None  # type: ignore[assignment]
    assert client.claim_task_messages("task_1") == []


def test_claim_task_messages_never_raises_on_api_error():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(*a, **k):
        raise QuestApiError(
            "Quest API POST /api/assistant-tasks/task_1/messages/claim -> 500: boom")

    client._request = failing_request  # type: ignore[assignment]
    assert client.claim_task_messages("task_1") == []


def test_claim_task_messages_never_raises_when_not_configured():
    from quest_ai_runner.runner.quest_client import QuestNotConfigured

    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def unconfigured(*a, **k):
        raise QuestNotConfigured("no api key")

    client._request = unconfigured  # type: ignore[assignment]
    assert client.claim_task_messages("task_1") == []
