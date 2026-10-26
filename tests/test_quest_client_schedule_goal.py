"""QuestClient.schedule_goal sets a goal's day and time via scheduled_start, validated client-side."""
import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_body():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured.update(method=method, path=path, body=body)
        return {"id": "goal_1"}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_schedule_goal_puts_scheduled_start_and_date():
    client, captured = client_capturing_body()
    out = client.schedule_goal("goal_1", date="2026-10-26", time="18:35")
    assert out == {"id": "goal_1"}
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/planning/goals/goal_1"
    assert captured["body"] == {"scheduled_start": "2026-10-26T18:35:00", "scheduled_date": "2026-10-26"}


@pytest.mark.parametrize("date", ["2026-10-6", "26-10-2026", "2026_10", "", None])
def test_schedule_goal_rejects_bad_date_without_a_request(date):
    client, captured = client_capturing_body()
    with pytest.raises(QuestApiError):
        client.schedule_goal("goal_1", date=date, time="18:35")
    assert captured == {}


@pytest.mark.parametrize("time", ["6:35 PM", "24:00", "18:60", "1835", "", None])
def test_schedule_goal_rejects_bad_time_without_a_request(time):
    client, captured = client_capturing_body()
    with pytest.raises(QuestApiError):
        client.schedule_goal("goal_1", date="2026-10-26", time=time)
    assert captured == {}


def test_schedule_goal_is_discovered_as_a_write_method():
    from quest_ai_runner.cli_quest import _quest_callable_methods, _quest_method_is_read_only
    assert "schedule_goal" in _quest_callable_methods()
    assert not _quest_method_is_read_only("schedule_goal")
