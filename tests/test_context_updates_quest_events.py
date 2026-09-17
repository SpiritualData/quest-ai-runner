"""``QuestEventsSource``: what the quest's PERSON actually did to it, read from analytics_events.

The gap this closes: ``QuestNotesSource`` sees a person's words and ``GoalUpdatesSource`` sees
their check-ins, but neither sees a person just doing the thing -- ticking a milestone done in
the app, rewriting the outcome, moving the deadline. None of that writes a note or an update, so
an autopilot pass reading only those two channels would see nothing and could re-propose work the
person already finished. What this file pins down:

  * An empty result (no events, or no method on the client) contributes nothing and never raises.
  * A ``quest_updated`` row renders with WHICH field changed (from ``event_data.updated_fields``),
    not a bare "the quest was updated" -- see ``render_quest_event``'s own docstring for why that
    distinction is the whole point of this source.
  * The cap (``MAX_OPEN_PER_SOURCE``, the same one ``QuestNotesSource``/``GoalUpdatesSource`` use)
    is passed straight through as the endpoint's own ``limit``, so a chatty quest never costs more
    than one small request.
  * The actor filter is always ``"app"`` -- this source can only ever report a PERSON's own
    change, never an API-key or AI write read back as one.
  * ``quest_events`` reaches every quest without being asked for, the same way ``quest_notes`` and
    ``goal_updates`` do.

Offline, driven against fakes.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.context_updates import (
    DEFAULT_ALWAYS,
    MAX_OPEN_PER_SOURCE,
    UpdateEngine,
    Watermarks,
)

NOW = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


class BareClient:
    """A client with none of the optional read methods: the source finds nothing."""


def _event(event_type, event_data=None, when=None, actor="app", user_id="u1"):
    return {
        "event_type": event_type,
        "event_data": event_data or {},
        "timestamp": when or _iso(),
        "user_id": user_id,
        "actor": actor,
    }


class QuestEventsClient(BareClient):
    """Has ``list_quest_events``, and records exactly what it was asked for."""

    def __init__(self, events):
        self._events = list(events)
        self.calls = []

    def list_quest_events(self, quest_id, *, since=None, limit=20, actor=None):
        self.calls.append({"quest_id": quest_id, "since": since, "limit": limit, "actor": actor})
        return list(self._events)


def _engine(client, **kwargs):
    kwargs.setdefault("always", ("quest_events",))
    kwargs.setdefault("now_fn", _now)
    return UpdateEngine(client, **kwargs)


def test_no_events_contributes_nothing():
    bundle = _engine(QuestEventsClient([])).collect({"quest_id": "q1"}, card_id="q1")

    assert [u for u in bundle.updates if u.source == "quest_events"] == []
    report = next(r for r in bundle.reports if r.source == "quest_events")
    assert report.ok
    assert report.found == 0


def test_quest_updated_renders_with_the_field_name_not_a_bare_update_line():
    client = QuestEventsClient([
        _event("quest_updated", {"updated_fields": ["outcome"]}),
    ])
    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    rows = [u for u in bundle.updates if u.source == "quest_events"]
    assert len(rows) == 1
    assert "outcome" in rows[0].body
    assert rows[0].body == "Changed the outcome field"
    assert rows[0].excerpt == rows[0].body
    assert rows[0].kind == "quest event"
    assert rows[0].needs_response is False


def test_quest_updated_with_multiple_fields_names_all_of_them():
    client = QuestEventsClient([
        _event("quest_updated", {"updated_fields": ["outcome", "current_state"]}),
    ])
    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    row = next(u for u in bundle.updates if u.source == "quest_events")
    assert row.body == "Changed the outcome, current_state fields"


def test_milestone_completed_names_the_milestone():
    client = QuestEventsClient([
        _event("quest_milestone_completed",
              {"milestone_id": "m1", "milestone_name": "Run 5k"}),
    ])
    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    row = next(u for u in bundle.updates if u.source == "quest_events")
    assert "Run 5k" in row.body


def test_an_event_older_than_the_watermark_is_never_asked_for():
    """The since passed to the client comes straight from the watermark: this source trusts the
    endpoint's own filtering rather than re-filtering client side (unlike GoalUpdatesSource,
    which has no server-side ``since`` to lean on)."""
    client = QuestEventsClient([_event("quest_completed")])
    marks = Watermarks(None)
    marks.set("q1", "quest_events", NOW - timedelta(hours=3))

    _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    assert client.calls[0]["since"] == (NOW - timedelta(hours=3)).isoformat()


def test_the_actor_filter_is_always_app():
    """This source can only ever report a PERSON's own change -- an API-key write (an AI task,
    autopilot, this very runner) authenticates as the account owner and is indistinguishable from
    a person's own edit on user_id alone, so it must never be asked for without the actor pin."""
    client = QuestEventsClient([_event("quest_completed")])

    _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    assert client.calls[0]["actor"] == "app"


def test_the_cap_is_passed_through_as_the_endpoints_own_limit():
    client = QuestEventsClient([_event("quest_completed") for _ in range(3)])

    _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    assert client.calls[0]["limit"] == MAX_OPEN_PER_SOURCE


def test_a_client_missing_the_method_yields_no_rows_and_no_exception():
    bundle = _engine(BareClient()).collect({"quest_id": "q1"}, card_id="q1")
    assert [u for u in bundle.updates if u.source == "quest_events"] == []


def test_quest_events_is_in_the_default_always_on_set_and_described():
    assert "quest_events" in DEFAULT_ALWAYS

    engine = UpdateEngine(BareClient(), now_fn=_now)
    described = engine.describe_sources()
    assert "quest_events" in described
    assert described["quest_events"]                        # non-empty, a real description

    specs = engine.specs_for({"quest_id": "q1"})
    assert {"source": "quest_events"} in specs               # reaches a card that asked for nothing
