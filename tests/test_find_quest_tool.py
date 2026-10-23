"""The read-only ``find_quest`` tool: the way out of "No quest is open in this run"."""
from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.core.tools import ToolContext

CREDS = {"QUEST_BASE_URL": "https://example.invalid", "QUEST_API_KEY": "key"}


class FakeClient:
    def list_teams(self):
        return [{"team_id": "t1"}, {"team_id": "t2"}]

    def list_quests(self, team_id=None):
        return {"t1": [{"quest_id": "quest_a", "outcome": "Reach 1,000 Subscribers"}],
                "t2": [{"quest_id": "quest_b", "outcome": "Psychic certification", "completed": True}]}[team_id]


def call(**args):
    return ops.find_quest_spec(CREDS, client_factory=FakeClient).handler(args, ToolContext())


def test_finds_by_words_across_teams():
    result = call(name="subscribers 1,000")
    assert result.ok and [r["quest_id"] for r in result.data] == ["quest_a"]


def test_empty_name_lists_all_and_marks_completed():
    result = call()
    assert "quest_b (completed)" in result.text and "quest_a" in result.text


def test_no_match_is_an_honest_failure():
    result = call(name="nonexistent")
    assert not result.ok and "2 quests checked" in result.text


def test_dead_end_message_points_to_find_quest():
    assert "find_quest" in ops.NO_QUEST.text
