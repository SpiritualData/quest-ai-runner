"""QuestClient.start_quest / attach_quest_to_team / edit_quest_field.

edit_quest_field previously sent {"fields": {...}} to PATCH /api/quests/{quest_id}/field, but the
backend handler only ever read a legacy singular {"field_name", "value"} body and never looked at
a "fields" key -- so every call through it silently wrote nothing. This file locks in the fixed
contract (one PATCH per field) alongside the two new quest-creation methods, generalized out of a
live need (creating a Quest with just outcome/current_state/purpose, no strategy or plan phase).
"""
import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient


def client_capturing_calls():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return {"id": "quest_1", "quest_id": "quest_1"}

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


# --- start_quest -------------------------------------------------------------

def test_start_quest_requires_only_category_id():
    client, calls = client_capturing_calls()
    client.start_quest(category_id="cat_career")
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/quests/start"
    body = calls[0]["body"]
    assert body["category_id"] == "cat_career"
    assert body["creation_mode"] == "quick"
    assert "outcome" not in body
    assert "current_state" not in body


def test_start_quest_passes_context_fields_when_given():
    client, calls = client_capturing_calls()
    client.start_quest(
        category_id="cat_career",
        outcome="The channel reaches a sustainable audience",
        current_state="Just decided the niche and format",
        acceptance_criteria="Six pilot videos published",
        linked_domain_ids=["freelance_growth_strategies"],
        timeline_days=730,
    )
    body = calls[0]["body"]
    assert body["outcome"] == "The channel reaches a sustainable audience"
    assert body["current_state"] == "Just decided the niche and format"
    assert body["acceptance_criteria"] == "Six pilot videos published"
    assert body["linked_domain_ids"] == ["freelance_growth_strategies"]
    assert body["timeline_days"] == 730


def test_start_quest_creation_mode_overridable():
    client, calls = client_capturing_calls()
    client.start_quest(category_id="cat_career", creation_mode="ai_assisted")
    assert calls[0]["body"]["creation_mode"] == "ai_assisted"


def test_start_quest_raises_instead_of_swallowing_api_errors():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError("Quest API POST /api/quests/start -> 422: bad category")

    client._request = failing_request  # type: ignore[assignment]
    with pytest.raises(QuestApiError):
        client.start_quest(category_id="cat_career")


# --- attach_quest_to_team -----------------------------------------------------

def test_attach_quest_to_team_sends_quest_id_body():
    client, calls = client_capturing_calls()
    client.attach_quest_to_team("team_4d9053d22663", "quest_1")
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/teams/team_4d9053d22663/quest"
    assert calls[0]["body"] == {"quest_id": "quest_1"}


def test_attach_quest_to_team_raises_instead_of_swallowing_api_errors():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")

    def failing_request(method, path, *, params=None, body=None):
        raise QuestApiError("Quest API POST /api/teams/team_1/quest -> 403: not admin")

    client._request = failing_request  # type: ignore[assignment]
    with pytest.raises(QuestApiError):
        client.attach_quest_to_team("team_1", "quest_1")


# --- edit_quest_field ----------------------------------------------------------

def test_edit_quest_field_sends_legacy_field_name_value_body():
    client, calls = client_capturing_calls()
    client.edit_quest_field("quest_1", {"purpose": "Help people develop psychic ability"})
    assert len(calls) == 1
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["path"] == "/api/quests/quest_1/field"
    assert calls[0]["body"] == {"field_name": "purpose", "value": "Help people develop psychic ability"}
    # The old body shape must never be sent again -- the backend silently ignores it.
    assert "fields" not in calls[0]["body"]


def test_edit_quest_field_sends_one_patch_per_field():
    client, calls = client_capturing_calls()
    client.edit_quest_field("quest_1", {"outcome": "New outcome", "current_state": "New state"})
    assert len(calls) == 2
    bodies = [c["body"] for c in calls]
    assert {"field_name": "outcome", "value": "New outcome"} in bodies
    assert {"field_name": "current_state", "value": "New state"} in bodies


def test_edit_quest_field_stops_and_returns_empty_on_first_failure():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    seen = []

    def failing_after_first(method, path, *, params=None, body=None):
        seen.append(body)
        if len(seen) == 1:
            return {"quest_id": "quest_1"}
        raise QuestApiError("Quest API PATCH /api/quests/quest_1/field -> 400: bad field")

    client._request = failing_after_first  # type: ignore[assignment]
    result = client.edit_quest_field("quest_1", {"outcome": "A", "current_state": "B"})
    assert result == {}
    # Never silently reports the earlier successful write as the whole call's result.
    assert len(seen) == 2
