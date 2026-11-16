"""QuestClient's undated backlog, abandon and per-member Today methods: the right route, body and
failure shape. Same fake-transport pattern as test_quest_client_goal_updates.py (no network)."""
import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestApiError, QuestClient
from quest_ai_runner.body.internal_context.quest.client.quest_client_support import QuestWriteError


def client_capturing_call(response=None):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        captured.update(method=method, path=path, params=params, body=body)
        return response if response is not None else {}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def client_failing_request():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")

    def failing_request(method, path, *, params=None, body=None, timeout_override=None):
        raise QuestApiError(f"Quest API {method} {path} -> 500: boom")

    client._request = failing_request  # type: ignore[assignment]
    return client


def test_move_goal_to_backlog_patches_the_backlog_route():
    client, captured = client_capturing_call({"id": "goal_1", "backlogged": True})
    result = client.move_goal_to_backlog("goal_1")
    assert (captured["method"], captured["path"]) == ("PATCH", "/api/planning/goals/goal_1/backlog")
    assert result and result.get("backlogged") is True


def test_list_backlog_goals_returns_the_goals_and_narrows_by_quest():
    client, captured = client_capturing_call({"goals": [{"id": "goal_1", "name": "Later"}], "total": 1})
    assert client.list_backlog_goals() == [{"id": "goal_1", "name": "Later"}]
    assert (captured["method"], captured["path"], captured["params"]) == ("GET", "/api/planning/goals/backlog", None)
    client.list_backlog_goals(quest_id="quest_1")
    assert captured["params"] == {"quest_id": "quest_1"}


def test_list_backlog_goals_is_empty_on_an_error_or_an_unexpected_shape():
    assert client_failing_request().list_backlog_goals() == []
    client, _captured = client_capturing_call(["not", "a", "dict"])
    assert client.list_backlog_goals() == []


def test_schedule_goal_for_period_posts_level_and_start_and_refuses_bad_input():
    client, captured = client_capturing_call({"status": "success"})
    client.schedule_goal_for_period("goal_1", level="week", period_start="2026-11-16")
    assert (captured["method"], captured["path"]) == ("POST", "/api/planning/goals/goal_1/schedule")
    assert captured["body"] == {"level": "week", "period_start": "2026-11-16"}
    with pytest.raises(QuestWriteError):
        client.schedule_goal_for_period("goal_1", level="fortnight", period_start="2026-11-16")
    with pytest.raises(QuestWriteError):
        client.schedule_goal_for_period("goal_1", level="day", period_start="next tuesday")


def test_abandon_goal_patches_the_abandon_route():
    client, captured = client_capturing_call({"id": "goal_1", "abandoned": True})
    client.abandon_goal("goal_1")
    assert (captured["method"], captured["path"]) == ("PATCH", "/api/planning/goals/goal_1/abandon")


def test_show_habit_on_my_today_puts_the_callers_choice():
    client, captured = client_capturing_call({"id": "coll_1", "hiddenOnMyToday": True})
    client.show_habit_on_my_today("coll_1", show=False)
    assert (captured["method"], captured["path"], captured["body"]) == ("PUT", "/api/planning/habits/coll_1/my-today", {"show": False})


@pytest.mark.parametrize("call", [
    lambda c: c.move_goal_to_backlog("goal_1"),
    lambda c: c.schedule_goal_for_period("goal_1", level="day", period_start="2026-11-16"),
    lambda c: c.abandon_goal("goal_1"),
    lambda c: c.show_habit_on_my_today("coll_1", show=True),
])
def test_a_failed_write_raises_a_typed_error(call):
    with pytest.raises(QuestWriteError):
        call(client_failing_request())
