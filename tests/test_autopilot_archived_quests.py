"""An archived quest never gets an autopilot pass, even if its stored autopilot config is still on.

Defense in depth: the Quest backend turns the mode off when a quest is archived, but the runner
also reads the ``archived`` flag the API returns (on the team listing row and on the quest state)
and refuses to schedule or work such a quest.
"""
from quest_ai_runner.runner.autopilot import AutopilotPass

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest
from .test_autopilot_pass_task import FakePassClient, _poller


class ArchivedListingClient(FakeAutopilotClient):
    """The team listing carries ``archived`` (as the backend now returns it)."""

    def __init__(self, *, archived_ids=(), archived_on_state=(), **kwargs):
        super().__init__(**kwargs)
        self.archived_ids = set(archived_ids)
        self.archived_on_state = set(archived_on_state)

    def list_quests(self, *, team_id=None):
        rows = super().list_quests(team_id=team_id)
        return [{**row, "archived": row["quest_id"] in self.archived_ids} for row in rows]

    def get_quest_autopilot(self, quest_id):
        state = super().get_quest_autopilot(quest_id)
        if state:
            state["archived"] = quest_id in self.archived_on_state
        return state


def _goals():
    return {q: _goals_payload(("day", "2026-07-12", [_goal(f"g_{q}")])) for q in ("live", "gone")}


def test_team_pass_skips_a_quest_the_listing_marks_archived():
    client = ArchivedListingClient(quests=[_quest("live"), _quest("gone")], goals_by_quest=_goals(),
                                   archived_ids={"gone"})
    eligible = AutopilotPass(client, team_id="team1", now=_now)._eligible_quests()
    assert [q["quest_id"] for q in eligible] == ["live"]


def test_team_pass_skips_a_quest_whose_state_says_archived():
    client = ArchivedListingClient(quests=[_quest("live"), _quest("gone")], goals_by_quest=_goals(),
                                   archived_on_state={"gone"})
    eligible = AutopilotPass(client, team_id="team1", now=_now)._eligible_quests()
    assert [q["quest_id"] for q in eligible] == ["live"]


def test_per_quest_pass_declines_an_archived_quest_with_autopilot_still_on():
    client = ArchivedListingClient(quests=[_quest("gone", mode="act")], goals_by_quest=_goals(),
                                   archived_on_state={"gone"})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    assert passer._eligible_quests("gone") == []


def test_active_quest_is_still_eligible():
    client = ArchivedListingClient(quests=[_quest("live")], goals_by_quest=_goals())
    passer = AutopilotPass(client, team_id="team1", now=_now)
    assert [q["quest_id"] for q in passer._eligible_quests()] == ["live"]
    assert [q["quest_id"] for q in passer._eligible_quests("live")] == ["live"]


def test_poller_creates_no_pass_for_an_archived_quest_with_autopilot_on():
    client = FakePassClient(
        quests=[{"quest_id": "gone", "archived": True}, {"quest_id": "live"}],
        autopilot_by_quest={"gone": {"mode": "act"}, "live": {"mode": "act"}})
    poller = _poller(client)
    snapshot = poller._quest_schedule_snapshot()
    assert snapshot["gone"]["mode"] == "off"
    assert snapshot["live"]["mode"] == "act"
    poller._ensure_autopilot_pass()
    assert [c.get("goal_id") for c in client.created] == ["live"]


def test_poller_treats_a_state_level_archived_flag_the_same():
    class StateArchived(FakePassClient):
        def get_quest_autopilot(self, quest_id):
            state = super().get_quest_autopilot(quest_id)
            state["archived"] = quest_id == "gone"
            return state

    client = StateArchived(quests=[{"quest_id": "gone"}],
                           autopilot_by_quest={"gone": {"mode": "suggest"}})
    _poller(client)._ensure_autopilot_pass()
    assert client.created == []
