"""The read-only ``find_quest`` tool: the way out of "No quest is open in this run"."""
from quest_ai_runner.the_body.hands.quest_operations import tools_quest_ops as ops
from quest_ai_runner.the_world.tools.tools import ToolContext

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


class OwnAndTeamClient(FakeClient):
    """An account with two quests of its own (one also on a team) and team quests listed with a name and no outcome."""

    def list_my_quests(self):
        return [{"quest_id": "quest_own", "state": {"name": "Psychic ability demonstration", "vision_statement": None}},
                {"quest_id": "quest_a", "state": {"name": "Subscribers", "vision_statement": "Reach 1,000 Subscribers"}}]

    def list_quests(self, team_id=None):
        return {"t1": [{"quest_id": "quest_a", "outcome": "Reach 1,000 Subscribers"}],
                "t2": [{"quest_id": "quest_reno", "outcome": None, "name": "Kids' bathroom renovation"}]}[team_id]


def test_own_quests_are_listed_with_team_quests_once_each_and_every_quest_carries_a_name():
    result = ops.find_quest_spec(CREDS, client_factory=OwnAndTeamClient).handler({}, ToolContext())
    assert result.ok and sorted(r["quest_id"] for r in result.data) == ["quest_a", "quest_own", "quest_reno"]
    assert "quest_own: Psychic ability demonstration" in result.text and "quest_reno: Kids' bathroom renovation" in result.text
    assert "quest_a: Subscribers (Reach 1,000 Subscribers)" in result.text
    found = ops.find_quest_spec(CREDS, client_factory=OwnAndTeamClient).handler({"name": "psychic"}, ToolContext())
    assert found.ok and [r["quest_id"] for r in found.data] == ["quest_own"]
