"""QuestClient's generic Quest planning + AI-rep-registry methods.

Generalized 2026-09-13 out of the personal lane's script (personal_quest.py / character_reps.py),
per this repo's Hard Rule #4: a second consumer's need belongs in the library, not copied into a
consumer. Same test shape as test_quest_client_create_task.py: a fake ``_request`` captures the
call, no network.
"""
import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient, QuestNotConfigured


def client_capturing_call():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return captured.get("_response", {})

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def client_failing_request():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError(f"Quest API {method} {path} -> 500: boom")

    client._request = failing_request  # type: ignore[assignment]
    return client


# --- list_current_goals ---------------------------------------------------------------------

def test_list_current_goals_hits_the_right_route():
    client, captured = client_capturing_call()
    captured["_response"] = {"quests": [{"quest_id": "q1", "goals": []}]}
    result = client.list_current_goals()
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/planning/goals/current/all"
    assert result == {"quests": [{"quest_id": "q1", "goals": []}]}


def test_list_current_goals_returns_empty_dict_on_failure():
    client = client_failing_request()
    assert client.list_current_goals() == {}


# --- get_day_plan ----------------------------------------------------------------------------

def test_get_day_plan_passes_for_date_when_given():
    client, captured = client_capturing_call()
    captured["_response"] = {"date": "2026-09-13", "goals": []}
    client.get_day_plan(date="2026-09-13")
    assert captured["path"] == "/api/daily-plan/yesterday-goals"
    assert captured["params"] == {"for_date": "2026-09-13"}


def test_get_day_plan_omits_params_when_no_date():
    client, captured = client_capturing_call()
    captured["_response"] = {}
    client.get_day_plan()
    assert captured["params"] is None


def test_get_day_plan_returns_empty_dict_on_failure():
    client = client_failing_request()
    assert client.get_day_plan(date="2026-09-13") == {}


# --- list_all_plans ----------------------------------------------------------------------------

def test_list_all_plans_unwraps_bare_list():
    client, captured = client_capturing_call()
    captured["_response"] = [{"id": "g1"}, {"id": "g2"}]
    assert client.list_all_plans() == [{"id": "g1"}, {"id": "g2"}]
    assert captured["path"] == "/api/planning/plans/all"


def test_list_all_plans_unwraps_plans_key():
    client, captured = client_capturing_call()
    captured["_response"] = {"plans": [{"id": "g1"}]}
    assert client.list_all_plans() == [{"id": "g1"}]


def test_list_all_plans_returns_empty_list_on_failure():
    client = client_failing_request()
    assert client.list_all_plans() == []


# --- list_habits -------------------------------------------------------------------------------

def test_list_habits_hits_the_right_route_and_unwraps():
    client, captured = client_capturing_call()
    captured["_response"] = {"habits": [{"id": "h1"}]}
    assert client.list_habits() == [{"id": "h1"}]
    assert captured["path"] == "/api/planning/habits/all"


def test_list_habits_returns_empty_list_on_failure():
    client = client_failing_request()
    assert client.list_habits() == []


# --- carry_over_day_plan -----------------------------------------------------------------------

def test_carry_over_day_plan_posts_to_the_right_route():
    client, captured = client_capturing_call()
    captured["_response"] = {"carried_over": 3}
    result = client.carry_over_day_plan()
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/daily-plan/carry-over"
    assert result == {"carried_over": 3}


def test_carry_over_day_plan_returns_empty_dict_on_failure():
    client = client_failing_request()
    assert client.carry_over_day_plan() == {}


# --- delete_goal ---------------------------------------------------------------------------

def test_delete_goal_sends_delete_and_returns_true():
    client, captured = client_capturing_call()
    assert client.delete_goal("goal_1") is True
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/planning/goals/goal_1"


def test_delete_goal_returns_false_on_failure():
    client = client_failing_request()
    assert client.delete_goal("goal_1") is False


# --- list_team_reps ----------------------------------------------------------------------------

def test_list_team_reps_uses_client_team_id_by_default():
    client, captured = client_capturing_call()
    captured["_response"] = {"reps": [{"rep_id": "rep_1", "display_name": "Wadona"}]}
    result = client.list_team_reps()
    assert captured["path"] == "/api/teams/team_1/reps"
    assert result == [{"rep_id": "rep_1", "display_name": "Wadona"}]


def test_list_team_reps_honors_explicit_team_id():
    client, captured = client_capturing_call()
    captured["_response"] = {"reps": []}
    client.list_team_reps(team_id="team_9")
    assert captured["path"] == "/api/teams/team_9/reps"


def test_list_team_reps_returns_empty_list_on_failure():
    client = client_failing_request()
    assert client.list_team_reps() == []


# --- create_rep ----------------------------------------------------------------------------

def test_create_rep_builds_person_less_body_by_default():
    client, captured = client_capturing_call()
    captured["_response"] = {"rep_id": "rep_1"}
    result = client.create_rep("Wadona", persona="COO persona", area="HQ character (wadona)")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/reps"
    body = captured["body"]
    assert body["display_name"] == "Wadona"
    assert body["teams"] == ["team_1"]
    assert body["persona"] == "COO persona"
    assert body["area"] == "HQ character (wadona)"
    assert "owner_user_id" not in body
    assert result == {"rep_id": "rep_1"}


def test_create_rep_includes_owner_user_id_when_given():
    client, captured = client_capturing_call()
    captured["_response"] = {"rep_id": "rep_2"}
    client.create_rep("Real Person", owner_user_id="user_5")
    assert captured["body"]["owner_user_id"] == "user_5"


def test_create_rep_raises_instead_of_swallowing_api_errors():
    client = client_failing_request()
    with pytest.raises(QuestApiError):
        client.create_rep("Wadona")


def test_create_rep_raises_when_no_team_id_available():
    client = QuestClient("https://quest.example", "test-api-key")
    with pytest.raises(QuestNotConfigured):
        client.create_rep("Wadona")


# --- list_decisions_for_quest ----------------------------------------------------------------

def test_list_decisions_for_quest_returns_all_by_default():
    client, captured = client_capturing_call()
    captured["_response"] = [
        {"id": "d1", "status": "open"},
        {"id": "d2", "status": "resolved"},
    ]
    result = client.list_decisions_for_quest("quest_1")
    assert captured["path"] == "/api/teams/decisions/for-quest"
    assert captured["params"] == {"quest_id": "quest_1"}
    assert len(result) == 2


def test_list_decisions_for_quest_filters_by_status_client_side():
    client, captured = client_capturing_call()
    captured["_response"] = [
        {"id": "d1", "status": "open"},
        {"id": "d2", "status": "resolved"},
    ]
    result = client.list_decisions_for_quest("quest_1", status="resolved")
    assert result == [{"id": "d2", "status": "resolved"}]


def test_list_decisions_for_quest_unwraps_decisions_key():
    client, captured = client_capturing_call()
    captured["_response"] = {"decisions": [{"id": "d1", "status": "open"}]}
    result = client.list_decisions_for_quest("quest_1")
    assert result == [{"id": "d1", "status": "open"}]


def test_list_decisions_for_quest_returns_empty_list_on_failure():
    client = client_failing_request()
    assert client.list_decisions_for_quest("quest_1") == []
