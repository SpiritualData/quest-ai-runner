"""QuestClient's goal-updates methods -- the per-goal check-in thread.

The reference backend has no single-goal route and no ``.../goals/{goal_id}/notes`` route (both
verified live: they 404 on every call). The real per-goal thread is ``goal_updates``:
  GET    /api/planning/goals/{goal_id}/updates
  POST   /api/planning/goals/{goal_id}/updates
  DELETE /api/planning/goals/{goal_id}/updates/{update_id}
  GET    /api/planning/quests/{quest_id}/goal-updates   (bulk path, being added in parallel)

This file also covers the resulting fallbacks in ``get_goal`` (scans ``list_quest_goals``) and
``list_goal_notes`` (falls back to ``list_goal_updates``).

Same fake-transport pattern as test_quest_client_create_goal.py / test_quest_client_planning_and_
reps.py: a fake ``_request`` captures/answers the call, no network, no real ids.
"""
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_call():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    captured = {}

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return captured.get("_response", {})

    client._request = fake_request  # type: ignore[assignment]
    return client, captured


def client_failing_request():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")

    def failing_request(method, path, *, params=None, body=None, timeout_override=None):
        raise QuestApiError(f"Quest API {method} {path} -> 500: boom")

    client._request = failing_request  # type: ignore[assignment]
    return client


# --- list_goal_updates -----------------------------------------------------------------------

def test_list_goal_updates_hits_the_right_route_with_params():
    client, captured = client_capturing_call()
    captured["_response"] = {"updates": [{"updateId": "gupd_1", "goalId": "goal_test1"}]}
    result = client.list_goal_updates("goal_test1", limit=5, before="2026-09-14T00:00:00Z")
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/planning/goals/goal_test1/updates"
    assert captured["params"] == {"limit": 5, "before": "2026-09-14T00:00:00Z"}
    assert result == [{"updateId": "gupd_1", "goalId": "goal_test1"}]


def test_list_goal_updates_omits_before_when_not_given():
    client, captured = client_capturing_call()
    captured["_response"] = {"updates": []}
    client.list_goal_updates("goal_test1")
    assert captured["params"] == {"limit": 20}


def test_list_goal_updates_unwraps_envelope():
    client, captured = client_capturing_call()
    captured["_response"] = {"updates": [{"updateId": "gupd_1"}, {"updateId": "gupd_2"}]}
    result = client.list_goal_updates("goal_test1")
    assert result == [{"updateId": "gupd_1"}, {"updateId": "gupd_2"}]


def test_list_goal_updates_accepts_bare_list():
    client, captured = client_capturing_call()
    captured["_response"] = [{"updateId": "gupd_1"}]
    result = client.list_goal_updates("goal_test1")
    assert result == [{"updateId": "gupd_1"}]


def test_list_goal_updates_returns_empty_list_on_failure():
    client = client_failing_request()
    assert client.list_goal_updates("goal_test1") == []


# --- add_goal_update --------------------------------------------------------------------------

def test_add_goal_update_posts_expected_body():
    client, captured = client_capturing_call()
    captured["_response"] = {"updateId": "gupd_1", "note": "made progress", "shared": False}
    result = client.add_goal_update("goal_test1", "made progress")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/planning/goals/goal_test1/updates"
    assert captured["body"] == {"note": "made progress", "shared": False}
    assert result == {"updateId": "gupd_1", "note": "made progress", "shared": False}


def test_add_goal_update_passes_shared_true():
    client, captured = client_capturing_call()
    captured["_response"] = {"updateId": "gupd_2"}
    client.add_goal_update("goal_test1", "sharing this one", shared=True)
    assert captured["body"] == {"note": "sharing this one", "shared": True}


def test_add_goal_update_returns_empty_dict_on_failure():
    client = client_failing_request()
    assert client.add_goal_update("goal_test1", "note text") == {}


# --- delete_goal_update ------------------------------------------------------------------------

def test_delete_goal_update_sends_delete_and_returns_true():
    client, captured = client_capturing_call()
    assert client.delete_goal_update("goal_test1", "gupd_1") is True
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/planning/goals/goal_test1/updates/gupd_1"


def test_delete_goal_update_returns_false_on_failure():
    client = client_failing_request()
    assert client.delete_goal_update("goal_test1", "gupd_1") is False


# --- list_quest_goal_updates -------------------------------------------------------------------

def test_list_quest_goal_updates_bulk_path_groups_by_goal_id():
    client, captured = client_capturing_call()
    captured["_response"] = {
        "updates": [
            {"updateId": "gupd_1", "goalId": "goal_test1", "note": "a"},
            {"updateId": "gupd_2", "goalId": "goal_test2", "note": "b"},
            {"updateId": "gupd_3", "goalId": "goal_test1", "note": "c"},
        ]
    }
    result = client.list_quest_goal_updates("quest_test123", limit_per_goal=20)
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/planning/quests/quest_test123/goal-updates"
    # Both caps go on the wire: the server trims to `limit` first and per goal afterwards,
    # so a quest with many goals needs the overall cap raised off its default of 50.
    assert captured["params"] == {"limit_per_goal": 20, "limit": 200}
    assert result == {
        "goal_test1": [
            {"updateId": "gupd_1", "goalId": "goal_test1", "note": "a"},
            {"updateId": "gupd_3", "goalId": "goal_test1", "note": "c"},
        ],
        "goal_test2": [{"updateId": "gupd_2", "goalId": "goal_test2", "note": "b"}],
    }


def test_list_quest_goal_updates_falls_back_to_per_goal_fanout_on_404():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path == "/api/planning/quests/quest_test123/goal-updates":
            raise QuestApiError("Quest API GET .../goal-updates -> 404: not found")
        if path == "/api/teams/team_test1/quests/quest_test123/goals":
            return {
                "quest_id": "quest_test123",
                "period_groups": [
                    {"time_scope": "week", "goals": [{"id": "goal_test1"}, {"id": "goal_test2"}]}
                ],
            }
        if path == "/api/planning/goals/goal_test1/updates":
            return {"updates": [{"updateId": "gupd_1", "goalId": "goal_test1", "note": "a"}]}
        if path == "/api/planning/goals/goal_test2/updates":
            return {"updates": [{"updateId": "gupd_2", "goalId": "goal_test2", "note": "b"}]}
        raise AssertionError(f"unexpected call: {method} {path}")

    client._request = fake_request  # type: ignore[assignment]
    result = client.list_quest_goal_updates("quest_test123")
    assert result == {
        "goal_test1": [{"updateId": "gupd_1", "goalId": "goal_test1", "note": "a"}],
        "goal_test2": [{"updateId": "gupd_2", "goalId": "goal_test2", "note": "b"}],
    }


def test_list_quest_goal_updates_fallback_uses_given_goal_ids_and_skips_empties():
    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path == "/api/planning/quests/quest_test123/goal-updates":
            raise QuestApiError("Quest API GET .../goal-updates -> 404: not found")
        if path == "/api/planning/goals/goal_test1/updates":
            return {"updates": [{"updateId": "gupd_1", "goalId": "goal_test1"}]}
        if path == "/api/planning/goals/goal_test2/updates":
            return {"updates": []}
        raise AssertionError(f"unexpected call: {method} {path}")

    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    client._request = fake_request  # type: ignore[assignment]
    result = client.list_quest_goal_updates(
        "quest_test123", goal_ids=["goal_test1", "goal_test2"])
    # goal_test2 returned no updates, so it is skipped entirely rather than listed empty.
    assert result == {"goal_test1": [{"updateId": "gupd_1", "goalId": "goal_test1"}]}


def test_list_quest_goal_updates_returns_empty_dict_on_total_failure():
    client = client_failing_request()
    assert client.list_quest_goal_updates("quest_test123") == {}


# --- get_goal falls back to scanning list_quest_goals ------------------------------------------

def test_get_goal_falls_back_to_list_quest_goals_scan_when_direct_route_fails():
    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path == "/api/teams/team_test1/quests/quest_test123/goals/goal_test1":
            raise QuestApiError("Quest API GET .../goals/goal_test1 -> 404: not found")
        if path == "/api/teams/team_test1/quests/quest_test123/goals":
            return {
                "quest_id": "quest_test123",
                "period_groups": [
                    {
                        "time_scope": "week",
                        "goals": [
                            {"id": "goal_test1", "name": "Ship the thing",
                             "description": "The full brief text", "completed": False},
                            {"id": "goal_test2", "name": "Other goal"},
                        ],
                    }
                ],
            }
        raise AssertionError(f"unexpected call: {method} {path}")

    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    client._request = fake_request  # type: ignore[assignment]
    result = client.get_goal("goal_test1", quest_id="quest_test123")
    assert result["name"] == "Ship the thing"
    assert result["description"] == "The full brief text"
    assert result["completed"] is False


def test_get_goal_returns_empty_dict_when_not_found_in_scan():
    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path == "/api/teams/team_test1/quests/quest_test123/goals/goal_missing":
            raise QuestApiError("Quest API GET .../goals/goal_missing -> 404: not found")
        if path == "/api/teams/team_test1/quests/quest_test123/goals":
            return {"quest_id": "quest_test123", "period_groups": []}
        raise AssertionError(f"unexpected call: {method} {path}")

    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    client._request = fake_request  # type: ignore[assignment]
    assert client.get_goal("goal_missing", quest_id="quest_test123") == {}


def test_get_goal_returns_empty_dict_without_quest_id():
    client, captured = client_capturing_call()
    assert client.get_goal("goal_test1") == {}
    assert captured == {}  # never attempted any request -- nothing to scan without quest_id


# --- list_goal_notes falls back to list_goal_updates, mapped ------------------------------------

def test_list_goal_notes_falls_back_to_goal_updates_mapped_shape():
    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path == "/api/teams/team_test1/quests/quest_test123/goals/goal_test1/notes":
            raise QuestApiError("Quest API GET .../notes -> 404: not found")
        if path == "/api/planning/goals/goal_test1/updates":
            return {
                "updates": [
                    {"updateId": "gupd_1", "goalId": "goal_test1", "userId": "user_1",
                     "userName": "Joshua", "note": "made progress", "shared": False,
                     "createdAt": "2026-09-14T19:11:28.166244+00:00"},
                ]
            }
        raise AssertionError(f"unexpected call: {method} {path}")

    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    client._request = fake_request  # type: ignore[assignment]
    result = client.list_goal_notes("goal_test1", quest_id="quest_test123")
    assert result == [
        {
            "id": "gupd_1",
            "text": "made progress",
            "author_name": "Joshua",
            "created_at": "2026-09-14T19:11:28.166244+00:00",
        }
    ]
    # no author_kind is invented for the mapped shape
    assert "author_kind" not in result[0]


def test_list_goal_notes_falls_back_directly_when_no_quest_id():
    client, captured = client_capturing_call()
    captured["_response"] = {"updates": [{"updateId": "gupd_1", "note": "n", "userName": "Joshua",
                                          "createdAt": "2026-09-14T00:00:00Z"}]}
    result = client.list_goal_notes("goal_test1")
    assert captured["path"] == "/api/planning/goals/goal_test1/updates"
    assert result == [
        {"id": "gupd_1", "text": "n", "author_name": "Joshua",
         "created_at": "2026-09-14T00:00:00Z"}
    ]


def test_list_goal_notes_returns_empty_list_when_both_paths_fail():
    client = client_failing_request()
    assert client.list_goal_notes("goal_test1", quest_id="quest_test123") == []


# --- the fallback fan-out is BOUNDED ----------------------------------------------------------
# Why these two exist: on 2026-09-20 the bulk route 404'd (it is not on the deployed backend), the
# fallback fanned out over a 216-goal quest, and the resulting 429s took down the autopilot pass
# that was mid-flight -- its own POST /api/assistant-tasks came back rate-limited, so Joshua's
# Sunday brief was never created and never sent.

def _client_counting_fanout(goal_count, *, rate_limit_after=None):
    """A client whose bulk route 404s, counting how many per-goal calls the fallback makes."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_test1")
    calls = []

    def fake_request(method, path, *, params=None, body=None, timeout_override=None):
        if path.endswith("/goal-updates"):
            raise QuestApiError(f"Quest API GET {path} -> 404: Not Found", status=404)
        calls.append(path)
        if rate_limit_after is not None and len(calls) > rate_limit_after:
            raise QuestApiError(f"Quest API GET {path} -> 429: RATE_LIMITED", status=429)
        return {"updates": [{"updateId": f"gupd_{len(calls)}"}]}

    client._request = fake_request  # type: ignore[assignment]
    client.list_quest_goals = lambda quest_id, **kw: {  # type: ignore[assignment]
        "period_groups": [{"goals": [{"id": f"goal_{i}"} for i in range(goal_count)]}]}
    return client, calls


def test_fallback_fanout_is_capped_on_a_quest_with_many_goals():
    from quest_ai_runner.runner.quest_client import GOAL_UPDATE_FANOUT_CAP
    client, calls = _client_counting_fanout(GOAL_UPDATE_FANOUT_CAP + 50)
    grouped = client.list_quest_goal_updates("quest_test1")
    assert len(calls) == GOAL_UPDATE_FANOUT_CAP
    assert len(grouped) == GOAL_UPDATE_FANOUT_CAP


def test_fallback_fanout_stops_when_the_backend_rate_limits_it():
    client, calls = _client_counting_fanout(10, rate_limit_after=3)
    grouped = client.list_quest_goal_updates("quest_test1")
    assert len(calls) == 4          # three answered, the fourth refused and ended the fan-out
    assert len(grouped) == 3        # what was read is kept, not thrown away
