"""The dry-run guard for writes to a goal: even inside an allowed tool call, a PUT to a goal (or its /assign) goes only to a goal this run created, whose current title starts with a
title the case allows, or whose title is the exact title of the allowed call. Every other goal is a person's, and the write is blocked and reported as an unexpected write."""
import pytest

from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestApiError
from quest_ai_runner.the_world.tools import dry_run_writes as dry

TITLES = {"h1": "Submit the Quest app to Google Play and advance the written feature test pass", "k1": "P3 next step: week of 2026-11-02", "x1": "FB push post A"}


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv(dry.ENV, "1")
    dry.RECORDER.clear()
    dry.set_allowances([{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title~": "P3 "}}])
    dry.set_goal_title_lookup(lambda goal_id: TITLES.get(goal_id))
    yield
    dry.set_allowances([])
    dry.set_goal_title_lookup(None)
    dry.RECORDER.clear()


def put(goal_id, suffix=""):
    with dry.allow_scope(True):
        return dry.quest_exchange("PUT", f"/api/planning/goals/{goal_id}{suffix}", {"title": "x"})


def test_a_persons_goal_is_blocked_inside_an_allowed_call_and_reported_as_an_unexpected_write(guarded):
    for suffix in ("", "/assign"):
        with pytest.raises(QuestApiError, match="not a goal this case may change"):
            put("h1", suffix)
    assert len(dry.RECORDER.snapshot()["unexpected"]) == 2 and "UNEXPECTED WRITE blocked: PUT /api/planning/goals/h1" in dry.RECORDER.snapshot()["unexpected"][0]


def test_a_goal_whose_current_title_starts_with_the_allowed_title_is_allowed(guarded):
    assert put("k1") is None and put("k1", "/assign") is None and dry.RECORDER.snapshot()["unexpected"] == []


def test_the_exact_title_of_the_allowed_call_and_a_goal_created_by_this_run_are_allowed(guarded):
    dry.set_allowances([{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1"}}])
    with pytest.raises(QuestApiError):
        put("x1")
    dry.RECORDER.call_title("FB push post A")
    assert put("x1") is None
    dry.RECORDER.created_goal("new-1")
    assert put("new-1") is None


def test_a_goal_whose_title_cannot_be_read_is_not_changed_and_reads_and_other_paths_are_untouched(guarded):
    with pytest.raises(QuestApiError, match="could not be read"):
        put("unknown")
    dry.set_goal_title_lookup(None)
    with pytest.raises(QuestApiError):
        put("k1")
    with dry.allow_scope(True):
        assert dry.quest_exchange("GET", "/api/planning/goals/h1") is None
        assert dry.quest_exchange("POST", "/api/planning/goals", {"title": "P3 new"}) is None
        assert dry.quest_exchange("PUT", "/api/quests/q1/notes/n1", {}) is None


def test_the_tool_call_records_the_exact_title_and_a_created_goal_is_remembered(guarded):
    from quest_ai_runner.the_body.hands.quest_operations import goal_links

    class Client:
        def list_quest_goals(self, quest_id):
            return {"period_groups": [{"goals": []}]}

        def create_goal(self, title, **kw):
            return type("W", (), {"id": "made-1", "data": {"id": "made-1"}})()
    goal_links.ensure_goal(Client(), "q1", "P3 next step: week of 2026-11-09", "2026_W46", {}, None)
    assert "made-1" in dry.RECORDER.created_goals
    assert put("made-1") is None
