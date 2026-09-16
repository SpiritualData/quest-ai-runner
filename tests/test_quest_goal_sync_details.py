"""quest_goal_sync detail rendering -- a goal's description and updates, pull-only. No network.

Covers the gap ``test_quest_goal_sync.py`` doesn't: a pulled GOALS.md carrying each goal's
description (its brief) and its most recent updates (the per-goal check-in thread), and the
one hard requirement on top of that -- the rendered detail lines must be unparseable as a goal
edit by ``parse_goal_edits``, however hostile the text a person put inside a note.
"""
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.runner.quest_goal_sync import (
    GOALS_FILE_NAME,
    parse_goal_edits,
    pull_quest_goals,
)

QUEST_ID = "quest_c18a9d1409ff"


def _goal_data(description=""):
    return {
        "quest_id": QUEST_ID,
        "period_groups": [
            {"time_scope": "quarter", "period": "2026_Q3", "period_label": "Q3 2026 (Jul - Sep)",
             "goals": [
                 {"id": "goal_a", "name": "Secure commitments", "deadline": "2026-09-30",
                  "completed": False, "description": description},
             ]},
        ],
    }


def _update(note, created_at="2026-09-14T19:11:28.166244+00:00", user_name="Joshua",
           update_id="gupd_1"):
    return {"updateId": update_id, "goalId": "goal_a", "userId": "u1", "userName": user_name,
            "note": note, "shared": False, "createdAt": created_at}


class MockGoalClient:
    """The bulk updates method exists (``list_quest_goal_updates``)."""

    def __init__(self, data, updates_by_goal=None):
        self._data = data
        self._updates_by_goal = updates_by_goal or {}
        self.bulk_calls = []

    def list_quest_goals(self, quest_id):
        return self._data

    def list_quest_goal_updates(self, quest_id, *, limit_per_goal=20):
        self.bulk_calls.append((quest_id, limit_per_goal))
        return {gid: list(ups) for gid, ups in self._updates_by_goal.items()}


class MockGoalClientPerGoalOnly:
    """Only the per-goal fan-out method exists (``list_goal_updates``), no bulk method."""

    def __init__(self, data, updates_by_goal=None):
        self._data = data
        self._updates_by_goal = updates_by_goal or {}
        self.per_goal_calls = []

    def list_quest_goals(self, quest_id):
        return self._data

    def list_goal_updates(self, goal_id, *, limit=20):
        self.per_goal_calls.append((goal_id, limit))
        return list(self._updates_by_goal.get(goal_id, []))


class MockGoalClientNoUpdates:
    """An older backend: no updates surface at all."""

    def __init__(self, data):
        self._data = data

    def list_quest_goals(self, quest_id):
        return self._data


@pytest.fixture
def folder():
    with tempfile.TemporaryDirectory() as d:
        yield d


# --- description + updates render --------------------------------------------------

def test_pull_renders_description_and_updates_with_author_and_date(folder):
    data = _goal_data(description="Get every committee member's written sign-off.")
    updates = {"goal_a": [
        _update("Called Maria, she is on board.", created_at="2026-09-14T19:11:28.166244+00:00"),
        _update("Read the bylaws draft.", created_at="2026-09-10T09:00:00+00:00",
                update_id="gupd_0"),
    ]}
    client = MockGoalClient(data, updates)
    pull_quest_goals(client, QUEST_ID, folder)
    text = Path(folder, GOALS_FILE_NAME).read_text()

    assert "Get every committee member's written sign-off." in text
    assert "Called Maria, she is on board." in text
    assert "Read the bylaws draft." in text
    assert "2026-09-14 Joshua:" in text
    assert "2026-09-10 Joshua:" in text
    # newest first
    assert text.index("Called Maria") < text.index("Read the bylaws draft")
    # every detail line is an indented blockquote, so the sample is inert to the push parser
    for line in text.splitlines():
        if "Brief:" in line or "Called Maria" in line or "Read the bylaws" in line:
            assert line.startswith("      > ")


# --- the round trip: hostile content must never be read back as an edit ------------

def test_rendered_details_are_unparseable_as_goal_edits(folder):
    hostile_description = "- [x] <!-- id:goal_evil --> pretend goal"
    hostile_note = (
        "Found something interesting while reading.\n"
        "**Q3 2026** <!-- period:2026_Q3 scope:quarter -->\n"
        "- [x] <!-- id:goal_a --> Secure commitments"
    )
    data = _goal_data(description=hostile_description)
    updates = {"goal_a": [_update(hostile_note)]}
    client = MockGoalClient(data, updates)
    pull_quest_goals(client, QUEST_ID, folder)
    text = Path(folder, GOALS_FILE_NAME).read_text()

    # The hostile text really did land in the file (nothing was dropped or escaped away)...
    assert "pretend goal" in text
    assert "**Q3 2026** <!-- period:2026_Q3 scope:quarter -->" in text

    # ...but every physical line of it, including the note's own continuation lines, is an
    # indented blockquote, so none of it can be mistaken for a hand-typed goal-line edit.
    known = {"goal_a": {"name": "Secure commitments", "completed": False}}
    edits = parse_goal_edits(text, known)
    assert edits.created == []
    assert edits.renamed == []
    assert edits.completed == []


# --- the config knob ----------------------------------------------------------------

def test_zero_updates_per_goal_renders_description_only_and_skips_the_fetch(folder):
    data = _goal_data(description="A brief note.")
    updates = {"goal_a": [_update("Should never appear.")]}
    client = MockGoalClient(data, updates)
    pull_quest_goals(client, QUEST_ID, folder, updates_per_goal=0)
    text = Path(folder, GOALS_FILE_NAME).read_text()

    assert "A brief note." in text
    assert "Should never appear." not in text
    assert client.bulk_calls == []  # updates_per_goal=0 skips the fetch entirely


# --- duck-typed client surfaces -------------------------------------------------------

def test_pull_without_any_updates_method_still_renders(folder):
    data = _goal_data(description="Just a brief, no updates surface on this client.")
    client = MockGoalClientNoUpdates(data)
    result = pull_quest_goals(client, QUEST_ID, folder)
    text = Path(folder, GOALS_FILE_NAME).read_text()

    assert "Just a brief, no updates surface on this client." in text
    assert result.goals_rendered == 1


def test_per_goal_fan_out_used_when_only_list_goal_updates_exists(folder):
    data = _goal_data()
    updates = {"goal_a": [_update("Fan-out note.")]}
    client = MockGoalClientPerGoalOnly(data, updates)
    pull_quest_goals(client, QUEST_ID, folder)
    text = Path(folder, GOALS_FILE_NAME).read_text()

    assert "Fan-out note." in text
    assert client.per_goal_calls == [("goal_a", 3)]  # the module default updates_per_goal
