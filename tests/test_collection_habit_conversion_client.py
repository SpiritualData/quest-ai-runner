"""QuestClient.convert_collection_to_habit / convert_habit_to_collection: the documented routes and bodies, offline (stubbed transport)."""
import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestApiError, QuestClient
from quest_ai_runner.body.internal_context.quest.client.quest_client_support import QuestWriteError


def client_capturing_calls(responses):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "body": body})
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        return value

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


def test_make_habit_sends_the_schedule_as_given():
    collection = {"id": "coll_1", "type": "habit"}
    client, calls = client_capturing_calls({"/api/data/collections/coll_1/make-habit": {"collection": collection, "entriesMarkedDone": 3}})
    assert client.convert_collection_to_habit("coll_1", time="21:30", days=["Mon"], markExistingDone=False)
    assert calls == [{"method": "POST", "path": "/api/data/collections/coll_1/make-habit",
                      "body": {"time": "21:30", "days": ["Mon"], "markExistingDone": False}}]


def test_stop_habit_posts_an_empty_body():
    client, calls = client_capturing_calls({"/api/data/collections/coll_1/stop-habit": {"collection": {"id": "coll_1", "type": None}}})
    assert client.convert_habit_to_collection("coll_1")
    assert calls == [{"method": "POST", "path": "/api/data/collections/coll_1/stop-habit", "body": {}}]


def test_a_refused_conversion_raises():
    client, _ = client_capturing_calls({"/api/data/collections/coll_1/stop-habit": QuestApiError("This collection is not a habit", status=409)})
    with pytest.raises(QuestWriteError):
        client.convert_habit_to_collection("coll_1")
