"""The standard ``update_quest_fields`` tool: the ONE path a quest field change may take.

Offline: a fake QuestClient records what the tool asked the API to do, so these assert the
contract (allowed fields, the structured ``user_requested`` verdict passed through untouched, and
an honest report when the backend turns the change into an ask instead of applying it) without a
network call.
"""
import pytest

from quest_ai_runner.core.tools import (
    QUEST_WRITABLE_FIELDS,
    ToolContext,
    build_tool_registry,
    update_quest_fields_spec,
)


class FakeQuestClient:
    """Records ``edit_quest_field`` calls and returns a scripted response."""

    def __init__(self, response=None):
        self.response = {"quest_id": "quest_abc"} if response is None else response
        self.calls = []

    def edit_quest_field(self, quest_id, fields, *, actor="ai", user_requested=False):
        self.calls.append({"quest_id": quest_id, "fields": dict(fields),
                           "actor": actor, "user_requested": user_requested})
        return self.response


def spec_with(client):
    return update_quest_fields_spec({}, client_factory=lambda: client)


def test_writes_an_allowed_field_as_the_ai_with_the_requested_verdict():
    client = FakeQuestClient()
    result = spec_with(client).handler(
        {"quest_id": "quest_abc", "fields": {"current_state": "Week 3, 12 miles"},
         "user_asked_for_this_field": True},
        ToolContext())
    assert result.ok
    assert client.calls == [{"quest_id": "quest_abc",
                             "fields": {"current_state": "Week 3, 12 miles"},
                             "actor": "ai", "user_requested": True}]
    assert "current_state" in result.text


def test_user_requested_defaults_to_false_when_not_declared():
    client = FakeQuestClient()
    spec_with(client).handler({"quest_id": "q1", "fields": {"outcome": "x"}}, ToolContext())
    assert client.calls[0]["user_requested"] is False


def test_quest_id_falls_back_to_the_turn_context():
    client = FakeQuestClient()
    spec_with(client).handler({"fields": {"purpose": "why"}},
                              ToolContext(quest_id="quest_from_ctx"))
    assert client.calls[0]["quest_id"] == "quest_from_ctx"


def test_no_quest_anywhere_writes_nothing():
    client = FakeQuestClient()
    result = spec_with(client).handler({"fields": {"purpose": "why"}}, ToolContext())
    assert not result.ok
    assert client.calls == []


@pytest.mark.parametrize("field", ["measurable_outcomes", "status", "plan", "strategies"])
def test_a_field_outside_the_allowlist_is_refused_without_calling_the_api(field):
    client = FakeQuestClient()
    result = spec_with(client).handler({"quest_id": "q1", "fields": {field: "x"}}, ToolContext())
    assert not result.ok
    assert field in result.text
    assert client.calls == []
    assert field not in QUEST_WRITABLE_FIELDS


def test_empty_fields_writes_nothing():
    client = FakeQuestClient()
    result = spec_with(client).handler({"quest_id": "q1", "fields": {}}, ToolContext())
    assert not result.ok
    assert client.calls == []


def test_a_change_held_for_approval_is_reported_as_not_applied():
    client = FakeQuestClient({"applied": False, "reason": "autopilot_off",
                              "decision_id": "teamdec_1"})
    result = spec_with(client).handler(
        {"quest_id": "q1", "fields": {"outcome": "new"}, "user_asked_for_this_field": True},
        ToolContext())
    assert result.ok                      # the call itself worked
    assert "NOT applied" in result.text   # but nothing was written
    assert "autopilot_off" in result.text


def test_a_failed_call_is_reported_as_nothing_written():
    client = FakeQuestClient({})
    result = spec_with(client).handler({"quest_id": "q1", "fields": {"outcome": "new"}},
                                       ToolContext())
    assert not result.ok
    assert "Nothing was written" in result.text


def test_it_is_a_standard_tool_whenever_quest_credentials_exist():
    registry = build_tool_registry({"QUEST_BASE_URL": "https://example.invalid",
                                    "QUEST_API_KEY": "key", "QAR_STANDARD_TOOLS": "1"})
    names = [s.name for s in registry.all()]
    assert "update_quest_fields" in names
    spec = [s for s in registry.all() if s.name == "update_quest_fields"][0]
    assert spec.mutates is True
    # The deep brief's tool block must point a coding worker at this instead of writing code.
    assert "never write code" in spec.when_to_use.lower()


def test_absent_without_quest_credentials():
    registry = build_tool_registry({"QAR_STANDARD_TOOLS": "1"})
    assert "update_quest_fields" not in [s.name for s in registry.all()]
