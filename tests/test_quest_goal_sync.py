"""quest_goal_sync — GOALS.md <-> a quest's goal ladder, against a MOCK client. No network."""
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.runner.quest_goal_sync import (
    GOALS_FILE_NAME,
    QuestGoalSyncError,
    parse_goal_edits,
    pull_quest_goals,
    push_goals_to_quest,
    render_goals_block,
    sync_quest_goals,
)

QUEST_ID = "quest_c18a9d1409ff"


def goal_data():
    return {
        "quest_id": QUEST_ID,
        "outcome": "Ship the thing",
        "period_groups": [
            {"time_scope": "quarter", "period": "2026_Q3", "period_label": "Q3 2026 (Jul - Sep)",
             "goals": [
                 {"id": "goal_a", "name": "Secure commitments", "deadline": "2026-09-30",
                  "completed": False},
                 {"id": "goal_b", "name": "Finish the paper", "completed": True},
             ]},
            {"time_scope": "week", "period": "2026_W35", "period_label": "Week 35",
             "goals": [{"id": "goal_c", "name": "Draft section one", "completed": False}]},
        ],
    }


class MockGoalClient:
    def __init__(self, data=None):
        self._data = data if data is not None else goal_data()
        self.completed = []
        self.updated = []
        self.created = []

    def list_quest_goals(self, quest_id):
        return self._data

    def set_goal_completed(self, goal_id, *, completed=True):
        self.completed.append((goal_id, completed))
        return {"ok": True}

    def update_goal(self, goal_id, fields):
        self.updated.append((goal_id, fields))
        return {"ok": True}

    def create_goal(self, title, *, period, quest_id=None, **kw):
        gid = f"goal_new{len(self.created) + 1}"
        self.created.append({"title": title, "period": period, "quest_id": quest_id})
        return {"id": gid}


@pytest.fixture
def folder():
    with tempfile.TemporaryDirectory() as d:
        yield d


# --- rendering ---------------------------------------------------------------

def test_render_groups_by_period_and_carries_ids():
    block = render_goals_block(goal_data())
    assert "### Quarter" in block and "### Week" in block
    assert "**Q3 2026 (Jul - Sep)** <!-- period:2026_Q3 scope:quarter -->" in block
    assert "- [ ] <!-- id:goal_a --> Secure commitments (due 2026-09-30)" in block
    assert "- [x] <!-- id:goal_b --> Finish the paper" in block


def test_render_keeps_completed_goals():
    """A plan that drops its finished rows reads as though the work was never scheduled."""
    assert "Finish the paper" in render_goals_block(goal_data())


def test_render_counts_are_stated():
    assert "_3 goal(s) across 2 period(s); 1 completed._" in render_goals_block(goal_data())


def test_render_empty_is_honest():
    assert "_(no goals yet)_" in render_goals_block({"period_groups": []})


# --- pull --------------------------------------------------------------------

def test_pull_writes_the_file_with_frontmatter(folder):
    result = pull_quest_goals(MockGoalClient(), QUEST_ID, folder)
    text = Path(folder, GOALS_FILE_NAME).read_text()
    assert text.startswith(f"---\nquest_id: {QUEST_ID}\n---")
    assert result.goals_rendered == 3
    assert "Secure commitments" in text


def test_pull_is_idempotent(folder):
    pull_quest_goals(MockGoalClient(), QUEST_ID, folder)
    before = Path(folder, GOALS_FILE_NAME).read_text()
    pull_quest_goals(MockGoalClient(), QUEST_ID, folder)
    assert Path(folder, GOALS_FILE_NAME).read_text() == before


def test_pull_preserves_prose_outside_the_managed_block(folder):
    path = Path(folder, GOALS_FILE_NAME)
    path.write_text("---\nquest_id: q\n---\n\nMy own notes about the plan.\n")
    pull_quest_goals(MockGoalClient(), QUEST_ID, folder)
    assert "My own notes about the plan." in path.read_text()


def test_pull_raises_when_goals_are_inaccessible(folder):
    class Broken:
        def list_quest_goals(self, quest_id):
            return {}
    with pytest.raises(QuestGoalSyncError):
        pull_quest_goals(Broken(), QUEST_ID, folder)


# --- parsing edits -----------------------------------------------------------

KNOWN = {"goal_a": {"name": "Secure commitments", "completed": False},
         "goal_b": {"name": "Finish the paper", "completed": True}}


def test_tick_becomes_a_completion():
    edits = parse_goal_edits("- [x] <!-- id:goal_a --> Secure commitments", KNOWN)
    assert edits.completed == ["goal_a"]


def test_already_complete_goal_is_not_re_sent():
    edits = parse_goal_edits("- [x] <!-- id:goal_b --> Finish the paper", KNOWN)
    assert edits.is_empty()


def test_unticking_never_reopens():
    """Inferring a reopen from a missing x would make a rendering hiccup a state change."""
    edits = parse_goal_edits("- [ ] <!-- id:goal_b --> Finish the paper", KNOWN)
    assert edits.is_empty()


def test_changed_text_becomes_a_rename():
    edits = parse_goal_edits("- [ ] <!-- id:goal_a --> Secure all commitments", KNOWN)
    assert edits.renamed == [("goal_a", "Secure all commitments")]


def test_due_date_is_not_part_of_the_title():
    edits = parse_goal_edits(
        "- [ ] <!-- id:goal_a --> Secure commitments (due 2026-09-30)", KNOWN)
    assert edits.is_empty()


def test_bullet_without_an_id_becomes_a_new_goal():
    text = "**Q3** <!-- period:2026_Q3 scope:quarter -->\n- [ ] A brand new goal"
    edits = parse_goal_edits(text, KNOWN)
    assert edits.created == [{"title": "A brand new goal", "period": "2026_Q3",
                              "scope": "quarter", "deadline": None}]


def test_new_bullet_with_no_period_heading_is_skipped_not_guessed():
    edits = parse_goal_edits("- [ ] Orphan goal", KNOWN)
    assert edits.is_empty()


def test_unknown_id_is_ignored():
    edits = parse_goal_edits("- [x] <!-- id:goal_zzz --> Who knows", KNOWN)
    assert edits.is_empty()


# --- push --------------------------------------------------------------------

def test_push_sends_completion_rename_and_creation(folder):
    client = MockGoalClient()
    pull_quest_goals(client, QUEST_ID, folder)
    path = Path(folder, GOALS_FILE_NAME)
    text = path.read_text()
    text = text.replace("- [ ] <!-- id:goal_a --> Secure commitments (due 2026-09-30)",
                        "- [x] <!-- id:goal_a --> Secure commitments (due 2026-09-30)")
    text = text.replace("- [ ] <!-- id:goal_c --> Draft section one",
                        "- [ ] <!-- id:goal_c --> Draft section two\n- [ ] Invent something new")
    path.write_text(text)

    result = push_goals_to_quest(client, QUEST_ID, folder)
    assert client.completed == [("goal_a", True)]
    assert client.updated[0][0] == "goal_c"
    assert client.created[0]["title"] == "Invent something new"
    assert client.created[0]["period"] == "2026_W35"
    assert result.changes == 3


def test_push_stamps_new_goals_with_their_id_so_a_repeat_is_a_noop(folder):
    client = MockGoalClient()
    pull_quest_goals(client, QUEST_ID, folder)
    path = Path(folder, GOALS_FILE_NAME)
    path.write_text(path.read_text().replace(
        "- [ ] <!-- id:goal_c --> Draft section one",
        "- [ ] <!-- id:goal_c --> Draft section one\n- [ ] Invent something new"))
    push_goals_to_quest(client, QUEST_ID, folder)
    assert "<!-- id:goal_new1 --> Invent something new" in path.read_text()

    # Second push: the bullet now carries an id Quest does not know, so nothing is re-created.
    push_goals_to_quest(client, QUEST_ID, folder)
    assert len(client.created) == 1


def test_push_with_no_edits_sends_nothing(folder):
    client = MockGoalClient()
    pull_quest_goals(client, QUEST_ID, folder)
    result = push_goals_to_quest(client, QUEST_ID, folder)
    assert result.changes == 0
    assert not client.completed and not client.updated and not client.created


def test_push_without_a_file_raises(folder):
    with pytest.raises(QuestGoalSyncError):
        push_goals_to_quest(MockGoalClient(), QUEST_ID, folder)


# --- direction ---------------------------------------------------------------

def test_both_pushes_before_it_pulls(folder):
    """Pulling first would regenerate the block and erase the tick before it was sent."""
    client = MockGoalClient()
    pull_quest_goals(client, QUEST_ID, folder)
    path = Path(folder, GOALS_FILE_NAME)
    path.write_text(path.read_text().replace(
        "- [ ] <!-- id:goal_a -->", "- [x] <!-- id:goal_a -->"))

    result = sync_quest_goals(client, QUEST_ID, folder, direction="both")
    assert client.completed == [("goal_a", True)]
    assert result.pulled and result.pushed


def test_unknown_direction_raises(folder):
    with pytest.raises(ValueError):
        sync_quest_goals(MockGoalClient(), QUEST_ID, folder, direction="sideways")
