"""QuestClient.create_goal must validate its period client-side and fail LOUDLY.

Mirrors test_quest_client_create_task.py's pattern: create_task exists to enqueue AI work;
create_goal is the sibling for the REAL, typed Goal object shown on a quest's plan (period-scoped,
with a deadline) -- distinct, and previously missing entirely (see runner/autopilot.py's
_maybe_create_goal, which degraded to a no-op because "QuestClient has no create_goal yet").
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
        return {"id": "goal_1", "deadline": "2026-08-18"}

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def test_create_goal_posts_to_planning_goals():
    client, captured = client_capturing_body()
    client.create_goal("Fill out the form", period="2026-08-18")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/planning/goals"
    assert captured["body"]["title"] == "Fill out the form"
    assert captured["body"]["period"] == "2026-08-18"


def test_create_goal_quest_id_is_optional():
    client, captured = client_capturing_body()
    client.create_goal("Standalone goal", period="2026-08-18")
    assert "quest_id" not in captured["body"]


def test_create_goal_includes_quest_id_when_given():
    client, captured = client_capturing_body()
    client.create_goal("Goal on a quest", period="2026-08-18", quest_id="quest_9")
    assert captured["body"]["quest_id"] == "quest_9"


@pytest.mark.parametrize("period", ["2026-08-18", "2026_W34", "2026_08", "2026_Q3", "2026"])
def test_create_goal_accepts_every_documented_period_format(period):
    client, captured = client_capturing_body()
    client.create_goal("t", period=period)
    assert captured["body"]["period"] == period


@pytest.mark.parametrize("period", ["not-a-period", "2026/08/18", "", "August 2026", "2026_W3"])
def test_create_goal_rejects_malformed_period_before_any_request(period):
    client, captured = client_capturing_body()
    with pytest.raises(QuestApiError):
        client.create_goal("t", period=period)
    assert captured == {}  # never reached _request -- fails fast, client-side


def test_create_goal_raises_instead_of_swallowing_api_errors():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError("Quest API POST /api/planning/goals -> 404: quest not found")

    client._request = failing_request  # type: ignore[assignment]
    with pytest.raises(QuestApiError):
        client.create_goal("t", period="2026-08-18")


def test_create_goal_passes_optional_fields_only_when_given():
    client, captured = client_capturing_body()
    client.create_goal(
        "t", period="2026-08-18",
        description="desc", criteria="crit", goal_type="day_goal",
        parent_goal_id="goal_parent", target_value=5.0, target_unit="hours",
        ai_help=True, assignee_rep_id="bailey",
    )
    body = captured["body"]
    assert body["description"] == "desc"
    assert body["criteria"] == "crit"
    assert body["goal_type"] == "day_goal"
    assert body["parent_goal_id"] == "goal_parent"
    assert body["target_value"] == 5.0
    assert body["target_unit"] == "hours"
    assert body["ai_help"] is True
    assert body["assignee_rep_id"] == "bailey"


def test_create_goal_omits_optional_fields_when_not_given():
    client, captured = client_capturing_body()
    client.create_goal("t", period="2026-08-18")
    body = captured["body"]
    for field in ("description", "criteria", "goal_type", "parent_goal_id",
                  "target_value", "target_unit", "ai_help", "assignee_rep_id"):
        assert field not in body
