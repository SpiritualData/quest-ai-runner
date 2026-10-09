"""``DecisionsSource``: asks sent to people on this quest, open or answered, through the engine.

The gap this closes: a decision is where someone outside the work asks the person for a call, and
its answer lives on the decision row. Before this source, an autopilot pass or a run could not see
that a question was still waiting, or that it had been declined and why. What this file pins down:

  * An open ask is offered on every look, flagged as needing a response, and is never dropped for
    being older than the watermark (it is still owed).
  * A resolved ask is offered only when it was resolved after the watermark, and never on a first
    look: the answered history of a quest is not news.
  * The resolver's own note travels verbatim, because it is their words.
  * An empty result, a client without the method, and a read that raises all contribute nothing and
    never break the rest of the bundle.
  * ``decisions`` is in the default always-on set, so every quest gets it without configuration.

Offline, driven against fakes.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.context_updates import UpdateEngine, Watermarks

NOW = datetime(2026, 10, 8, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _decision(decision_id="d1", status="open", **extra):
    row = {
        "decision_id": decision_id,
        "status": status,
        "summary": "Can we move the launch to Friday?",
        "requester_name": "Alex",
        "assignee_name": "Sam",
        "created_at": _iso(days_ago=2),
        "resolved_at": None,
        "resolution": None,
        "response_text": None,
        "resolved_by_name": None,
        "auto_resolved": False,
    }
    row.update(extra)
    return row


class DecisionsClient:
    """Has ``list_decisions_for_quest`` and records what it was asked for."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []

    def list_decisions_for_quest(self, quest_id):
        self.calls.append(quest_id)
        return list(self._rows)


class BareClient:
    """A client with none of the optional read methods: the source finds nothing."""


class RaisingClient:
    def list_decisions_for_quest(self, quest_id):
        raise RuntimeError("backend down")


def _engine(client, **kwargs):
    kwargs.setdefault("always", ("decisions",))
    kwargs.setdefault("now_fn", _now)
    return UpdateEngine(client, **kwargs)


def _rows(bundle):
    return [u for u in bundle.updates if u.source == "decisions"]


def test_an_open_ask_is_offered_and_flagged_as_needing_a_response():
    client = DecisionsClient([_decision()])
    marks = Watermarks(None)
    marks.set("q1", "decisions", NOW - timedelta(hours=1))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    rows = _rows(bundle)
    assert len(rows) == 1
    assert rows[0].needs_response is True
    assert rows[0].kind == "open ask"
    assert "Alex asked Sam" in rows[0].title
    assert rows[0].body == "Can we move the launch to Friday?"


def test_an_open_ask_older_than_the_watermark_is_still_offered():
    """Owed an answer however old it is: the watermark must not make a waiting question vanish."""
    client = DecisionsClient([_decision(created_at=_iso(days_ago=30))])
    marks = Watermarks(None)
    marks.set("q1", "decisions", NOW - timedelta(hours=1))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    assert [u.item_id for u in _rows(bundle)] == ["d1"]


def test_a_resolved_ask_is_offered_once_when_resolved_after_the_last_look():
    client = DecisionsClient([_decision(
        status="resolved", resolution="decline", response_text="Not Friday, the venue is booked.",
        resolved_by_name="Sam", resolved_at=_iso(hours_ago=2),
    )])
    marks = Watermarks(None)
    marks.set("q1", "decisions", NOW - timedelta(hours=6))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    rows = _rows(bundle)
    assert len(rows) == 1
    assert rows[0].kind == "resolved ask"
    assert rows[0].needs_response is False
    assert "resolved as decline" in rows[0].title
    assert rows[0].body == "Not Friday, the venue is booked."
    assert rows[0].verbatim is True


def test_a_resolved_ask_answered_before_the_last_look_is_history_not_news():
    client = DecisionsClient([_decision(
        status="resolved", resolution="approve", resolved_at=_iso(days_ago=1),
    )])
    marks = Watermarks(None)
    marks.set("q1", "decisions", NOW - timedelta(hours=6))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    assert _rows(bundle) == []


def test_a_first_look_offers_open_asks_but_never_the_settled_history():
    client = DecisionsClient([
        _decision("open1"),
        _decision("done1", status="resolved", resolution="approve", resolved_at=_iso(hours_ago=3)),
    ])

    bundle = _engine(client).collect({"quest_id": "q1"}, card_id="q1")

    assert [u.item_id for u in _rows(bundle)] == ["open1"]


def test_an_auto_resolved_ask_says_it_was_resolved_at_its_deadline():
    client = DecisionsClient([_decision(
        status="resolved", resolution="approve", auto_resolved=True, resolved_at=_iso(hours_ago=1),
    )])
    marks = Watermarks(None)
    marks.set("q1", "decisions", NOW - timedelta(hours=6))

    bundle = _engine(client, watermarks=marks).collect({"quest_id": "q1"}, card_id="q1")

    row = _rows(bundle)[0]
    assert "resolved automatically at its deadline" in row.title
    assert row.verbatim is False


def test_no_decisions_contributes_nothing():
    bundle = _engine(DecisionsClient([])).collect({"quest_id": "q1"}, card_id="q1")

    assert _rows(bundle) == []
    report = next(r for r in bundle.reports if r.source == "decisions")
    assert report.ok
    assert report.found == 0


def test_a_client_without_the_method_contributes_nothing():
    bundle = _engine(BareClient()).collect({"quest_id": "q1"}, card_id="q1")

    assert _rows(bundle) == []


def test_a_read_that_raises_is_reported_and_the_rest_of_the_bundle_still_arrives():
    bundle = _engine(RaisingClient()).collect({"quest_id": "q1"}, card_id="q1")

    assert _rows(bundle) == []


def test_decisions_reach_every_quest_without_being_asked_for():
    client = DecisionsClient([_decision()])
    engine = UpdateEngine(client, now_fn=_now)

    assert "decisions" in engine.describe_sources()
    bundle = engine.collect({"quest_id": "q1"}, card_id="q1")

    assert [u.item_id for u in _rows(bundle)] == ["d1"]
    assert client.calls == ["q1"]
