"""``GoalUpdatesSource``: check-ins the person wrote on this quest's individual goals.

The gap this closes: before it existed, nothing in this library ever read a goal update, on any
quest, so an autopilot pass saw a goal's name and deadline but never the person's own account of
where it actually stood. What this file pins down:

  * A new update is offered with the GOAL's title (not the quest's) as its location, and the
    person's own words as its body.
  * The watermark, not a hardcoded window, decides what is new -- same rule every other source
    in this engine follows.
  * The one-call route (``list_quest_goal_updates``) is preferred; a client without it still
    works through a per-goal fan-out (``list_goal_updates``).
  * A client with neither method is a gap, not a crash.
  * ``goal_updates`` reaches every quest without being asked for, the same way ``quest_notes`` and
    ``insights`` do.

Offline, driven against fakes.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.context_updates import DEFAULT_ALWAYS, UpdateEngine, Watermarks

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


class BareClient:
    """A client with none of the optional read methods: the source finds nothing."""


GOALS_PAYLOAD = {
    "quest_id": "q1",
    "outcome": "Finish the dissertation",
    "period_groups": [
        {"time_scope": "quarter", "period": "2026-Q3", "period_label": "This quarter",
         "goals": [
             {"id": "goal_1", "name": "Read Thagard (2005) and note key points",
              "description": "", "deadline": "2026-09-30", "completed": False},
             {"id": "goal_2", "name": "Draft chapter two", "description": "",
              "deadline": "2026-10-15", "completed": False},
         ]},
    ],
}


def _goal_update(update_id, goal_id, note, author="Joshua", when=None):
    return {"updateId": update_id, "goalId": goal_id, "userId": "u1", "userName": author,
            "note": note, "shared": False, "createdAt": when or _iso()}


class BulkGoalUpdatesClient(BareClient):
    """Has the one-call route, ``list_quest_goal_updates``."""

    def __init__(self, goals, updates_by_goal):
        self._goals = goals
        self._updates_by_goal = updates_by_goal
        self.bulk_calls = []

    def list_quest_goals(self, quest_id):
        return self._goals

    def list_quest_goal_updates(self, quest_id, *, limit_per_goal=20):
        self.bulk_calls.append((quest_id, limit_per_goal))
        return self._updates_by_goal


class FanoutGoalUpdatesClient(BareClient):
    """No ``list_quest_goal_updates`` -- only the per-goal route."""

    def __init__(self, goals, updates_by_goal):
        self._goals = goals
        self._updates_by_goal = updates_by_goal
        self.per_goal_calls = []

    def list_quest_goals(self, quest_id):
        return self._goals

    def list_goal_updates(self, goal_id, *, limit=20):
        self.per_goal_calls.append((goal_id, limit))
        return self._updates_by_goal.get(goal_id, [])


class GoalsOnlyClient(BareClient):
    """Knows the quest's goals but has neither update-fetching method -- the gap this source must
    survive without raising."""

    def __init__(self, goals):
        self._goals = goals

    def list_quest_goals(self, quest_id):
        return self._goals


def _engine(client, **kwargs):
    kwargs.setdefault("always", ("goal_updates",))
    kwargs.setdefault("now_fn", _now)
    return UpdateEngine(client, **kwargs)


def test_a_new_update_is_offered_with_the_goals_title_and_the_persons_words():
    client = BulkGoalUpdatesClient(GOALS_PAYLOAD, {
        "goal_1": [_goal_update("gupd_1", "goal_1",
                                "Finished the Thagard reading, taking notes now")],
    })
    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    updates = [u for u in bundle.updates if u.source == "goal_updates"]
    assert len(updates) == 1
    row = updates[0]
    assert row.location == "Read Thagard (2005) and note key points"
    assert row.body == "Finished the Thagard reading, taking notes now"
    assert row.excerpt == row.body
    assert row.author == "Joshua"
    assert "Joshua" in row.title and "Read Thagard" in row.title
    assert row.needs_response is False
    assert row.item_id == "gupd_1"
    assert row.kind == "goal update"
    assert client.bulk_calls == [("q1", 20)]


def test_an_update_older_than_the_watermark_is_not_offered():
    old = _goal_update("gupd_old", "goal_1", "Old check-in", when=_iso(days_ago=10))
    new = _goal_update("gupd_new", "goal_1", "Fresh check-in", when=_iso(hours_ago=1))
    client = BulkGoalUpdatesClient(GOALS_PAYLOAD, {"goal_1": [old, new]})
    marks = Watermarks(None)
    marks.set("q1", "goal_updates", NOW - timedelta(days=2))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    ids = {u.item_id for u in bundle.updates if u.source == "goal_updates"}
    assert ids == {"gupd_new"}


def test_the_per_goal_fanout_path_is_used_when_the_client_has_no_bulk_method():
    client = FanoutGoalUpdatesClient(GOALS_PAYLOAD, {
        "goal_1": [_goal_update("gupd_1", "goal_1", "Chapter outline done")],
        "goal_2": [],
    })
    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    updates = [u for u in bundle.updates if u.source == "goal_updates"]
    assert len(updates) == 1
    assert updates[0].item_id == "gupd_1"
    assert updates[0].location == "Draft chapter two" or updates[0].location == (
        "Read Thagard (2005) and note key points")
    assert sorted(goal_id for goal_id, _ in client.per_goal_calls) == ["goal_1", "goal_2"]


def test_a_client_missing_both_update_methods_yields_no_rows_and_no_exception():
    bundle = _engine(GoalsOnlyClient(GOALS_PAYLOAD)).collect({"quest_id": "q1"}, card_id="q1")
    assert [u for u in bundle.updates if u.source == "goal_updates"] == []

    bare = _engine(BareClient()).collect({"quest_id": "q1"}, card_id="q1")
    assert [u for u in bare.updates if u.source == "goal_updates"] == []


def test_goal_updates_is_in_the_default_always_on_set_and_described():
    assert "goal_updates" in DEFAULT_ALWAYS

    engine = UpdateEngine(BareClient(), now_fn=_now)
    described = engine.describe_sources()
    assert "goal_updates" in described
    assert described["goal_updates"]                       # non-empty, a real description

    specs = engine.specs_for({"quest_id": "q1"})
    assert {"source": "goal_updates"} in specs              # reaches a card that asked for nothing


def test_the_report_explains_when_updates_were_found_but_none_were_new():
    old = _goal_update("gupd_old", "goal_1", "Old check-in", when=_iso(days_ago=10))
    client = BulkGoalUpdatesClient(GOALS_PAYLOAD, {"goal_1": [old]})
    marks = Watermarks(None)
    marks.set("q1", "goal_updates", NOW - timedelta(days=2))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    report = next(r for r in bundle.reports if r.source == "goal_updates")
    assert report.found == 0
    assert report.considered == 1
    assert "none new" in report.explanation
