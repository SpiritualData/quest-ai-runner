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


# --- a quest on a team other than the client's own ---------------------------

def test_list_quest_goals_falls_back_to_the_quests_own_team(monkeypatch):
    """A quest moved off the lane's configured team must still sync its goals.

    Regression: the endpoint is team-scoped and the client always used its OWN team_id, so a quest
    living on any other team of the same owner 404'd on every scan, forever. An owner-scoped lane
    routinely spans teams, and a quest can be moved between them at any time.
    """
    import json
    import urllib.error
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    urls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        urls.append(url)
        if "/api/quests/me" in url:
            return _Resp([{"quest_id": "q1", "team_id": "team-owner"}])
        if "/api/teams/team-configured/" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "/api/teams/team-owner/" in url:
            return _Resp({"quest_id": "q1", "period_groups": [{"goals": [{"id": "g1"}]}]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test", team_id="team-configured")
    data = client.list_quest_goals("q1")

    assert data.get("period_groups"), "goals should come back from the quest's own team"
    assert any("/api/teams/team-configured/" in u for u in urls), "tries the configured team first"
    assert any("/api/teams/team-owner/" in u for u in urls), "retries on the quest's own team"

    # The resolution is cached: a second call must not re-list the owner's quests.
    before = sum(1 for u in urls if "/api/quests/me" in u)
    client.list_quest_goals("q1")
    after = sum(1 for u in urls if "/api/quests/me" in u)
    assert after == before, "the owning team is resolved once, then cached"


def test_list_quest_goals_honors_an_explicit_team(monkeypatch):
    """An explicitly named team is the caller's call: no second-guessing, no fallback."""
    import urllib.error
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    urls = []

    def _fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test", team_id="team-configured")
    assert client.list_quest_goals("q1", team_id="team-explicit") == {}
    assert all("/api/quests/me" not in u for u in urls), "never resolves an owner team"
    assert len(urls) == 1, "no retry when the caller named the team"


def goal_client_with_moving_quest(monkeypatch, urls, owner_team):
    """A QuestClient whose backend serves quest q1's goals ONLY on ``owner_team[0]``.

    ``owner_team`` is a one-element list so a test can move the quest mid-scenario; both the
    owner-scoped quest list and the team-scoped goals endpoint follow it, exactly like a real move.
    """
    import json
    import urllib.error
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        urls.append(url)
        if "/api/quests/me" in url:
            return FakeResp([{"quest_id": "q1", "team_id": owner_team[0]}] if owner_team[0] else [])
        if f"/api/teams/{owner_team[0]}/" in url and owner_team[0]:
            return FakeResp({"quest_id": "q1", "period_groups": [{"goals": [{"id": "g1"}]}]})
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return QuestClient("http://quest.example", "qsk_test", team_id="team-configured")


def test_list_quest_goals_survives_a_second_team_move(monkeypatch):
    """A quest moved to a SECOND team must sync again without a runner restart.

    Regression: _owning_team_for cached the quest's team for the life of the process and nothing
    ever invalidated it, so after the first fallback worked, a later move made both the configured
    team and the cached team 404, and list_quest_goals returned {} on every scan until restart. Now
    the cached team failing is read as "the quest moved again": the entry is dropped and resolved
    once more from the live quest list.
    """
    urls = []
    owner = ["team-owner"]
    client = goal_client_with_moving_quest(monkeypatch, urls, owner)

    assert client.list_quest_goals("q1").get("period_groups"), "first move: fallback works"
    lists_after_first = sum(1 for u in urls if "/api/quests/me" in u)
    assert lists_after_first == 1

    owner[0] = "team-second"  # the quest moves again while the runner keeps running
    data = client.list_quest_goals("q1")
    assert data.get("period_groups"), "second move: goals come back from the new team"
    assert any("/api/teams/team-owner/" in u for u in urls[len(urls) - 4:]), \
        "the cached team is tried first (that failure is the staleness signal)"
    assert any("/api/teams/team-second/" in u for u in urls), "then the freshly resolved team"
    assert sum(1 for u in urls if "/api/quests/me" in u) == lists_after_first + 1, \
        "exactly one re-resolution, not one per attempt"

    # The new resolution is cached in turn: a third scan does not list the owner's quests again.
    before = len(urls)
    assert client.list_quest_goals("q1").get("period_groups")
    tail = urls[before:]
    assert not any("/api/quests/me" in u for u in tail), "second team is now the cached team"
    assert tail == [f"http://quest.example/api/teams/team-configured/quests/q1/goals",
                    f"http://quest.example/api/teams/team-second/quests/q1/goals"]


def test_list_quest_goals_re_resolves_at_most_once_per_call(monkeypatch):
    """When the freshly resolved team ALSO fails, give up for this scan; never loop.

    The quest list can lag a move, so the re-resolution can still name a team that 404s. That is
    one failed scan (retried next scan as before), not a retry storm: one cached attempt, one
    fresh listing, one attempt on its answer, then {}.
    """
    import urllib.error
    import urllib.request

    urls = []
    owner = ["team-owner"]
    client = goal_client_with_moving_quest(monkeypatch, urls, owner)
    assert client.list_quest_goals("q1").get("period_groups")

    # The quest leaves team-owner; the quest list now says team-stale, but that team 404s too
    # (the list lags the move).
    owner[0] = "team-unlisted"
    real_urlopen = urllib.request.urlopen

    def lagging(req, timeout=None):
        if "/api/quests/me" in req.full_url:
            urls.append(req.full_url)
            import json

            class LaggingResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps([{"quest_id": "q1", "team_id": "team-stale"}]).encode()
            return LaggingResp()
        return real_urlopen(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", lagging)
    before = len(urls)
    assert client.list_quest_goals("q1") == {}
    tail = urls[before:]
    assert sum(1 for u in tail if "/api/quests/me" in u) == 1, "one fresh listing, no loop"
    assert sum(1 for u in tail if "/api/teams/" in u) == 3, \
        "configured, cached, freshly resolved: three attempts and out"

    # And the bad answer was NOT left behind as the cached team forever: the next scan lists again.
    before = len(urls)
    client.list_quest_goals("q1")
    assert any("/api/quests/me" in u for u in urls[before:]), \
        "a team that failed is not kept as the cached resolution"


def test_owning_team_miss_is_not_cached(monkeypatch):
    """A quest absent from the owner's list (or a failed listing) is asked again next time.

    Caching "" would freeze one transient listing failure into "this quest has no team" for the
    life of the process, the same restart-only bug in a different coat.
    """
    urls = []
    owner = [""]  # not in the owner's list yet
    client = goal_client_with_moving_quest(monkeypatch, urls, owner)
    assert client.list_quest_goals("q1") == {}
    assert client._quest_team_cache == {}, "a miss leaves no cache entry"

    owner[0] = "team-owner"  # now it is
    assert client.list_quest_goals("q1").get("period_groups"), "resolved on the next scan"
    assert client._quest_team_cache == {"q1": "team-owner"}
