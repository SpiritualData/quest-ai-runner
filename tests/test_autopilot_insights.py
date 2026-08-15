"""Autopilot sees what the person has CAPTURED since its last pass, and still works when it cannot.

The ask that produced this: "make sure that autopilot runs are aware of relevant insights since the
last time it ran, leveraging category field". Before it, ``grep -rn insight quest_ai_runner/``
found nothing — Quest had been storing every quick capture, with the person's own category tags,
in a collection no pass ever read.

Two things this file pins down beyond "the text shows up":

  * The cutoff is the quest's OWN ``autopilot.last_pass_at``, the same stamp the cadence gate reads,
    so "since the last time it ran" needs no second freshness tracker to fall out of step with.
  * NOTHING matches a category tag against a quest. The tags are rendered next to each insight and
    the reader decides — a fixed tag-to-quest rule would silently drop every wording it did not
    anticipate, which is what hard rule #3 forbids.

Driven against a fake client, offline. The last test is the compatibility one: a client with no
insights methods at all must compose exactly the batch it composed before.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    AutopilotPass,
    compose_batch_text,
    next_steps_from_pass,
)

from tests.test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _quest

NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _entry(entry_id, text, *, categories=None, acted_on=False, created_at="2026-07-12T08:00:00Z"):
    values = {"insight": text, "acted_on": acted_on}
    if categories is not None:
        values["categories"] = categories
    return {"id": entry_id, "fieldValues": values, "createdAt": created_at}


class CapturingClient(FakeAutopilotClient):
    """The autopilot fake plus the two insight reads, counting how often they are called."""

    def __init__(self, *args, entries=None, collection=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = list(entries or [])
        self.collection = collection if collection is not None else {"id": "coll_1"}
        self.entry_calls = []

    def get_insights_collection(self):
        return dict(self.collection)

    def list_collection_entries(self, collection_id, *, page=0, limit=50):
        self.entry_calls.append((collection_id, page))
        items = self.entries if page == 0 else []
        return {"items": items, "pagination": {"has_next": False}}


# --- the composed text ---------------------------------------------------------------------

def test_compose_batch_text_carries_the_insight_block_with_its_tags():
    block = ("Insights the person captured on Quest since 2026-07-11 and has not yet marked acted "
             "on, in their own words, with the category tags they chose:\n"
             "  - [2026-07-12] tagged dissertation\n      Mornings are the only writing time")
    text = compose_batch_text("ship the thing", [_goal("g1", "Draft chapter two")], insights=block)
    assert "Mornings are the only writing time" in text
    assert "tagged dissertation" in text


def test_compose_batch_text_is_unchanged_without_insights():
    goals = [_goal("g1", "Draft chapter two")]
    assert compose_batch_text("ship the thing", goals) == \
        compose_batch_text("ship the thing", goals, insights=None)


def test_next_steps_artifact_notes_the_capture_without_promoting_it_to_a_step():
    """The person captured it; they did not commit to it. Turning a note-to-self into a planned
    step is how the artifact stops being trustworthy."""
    steps = next_steps_from_pass([_goal("g1", "Draft chapter two")], scope_label="week:2026_W28",
                                 reflection_note="From their week review: protect two mornings.",
                                 insights_note="Unacted insight from 2026-07-12: batch the errands")
    assert steps.steps == ["Draft chapter two"]
    assert "protect two mornings" in steps.note
    assert "batch the errands" in steps.note


# --- the pass ------------------------------------------------------------------------------

def _pass_with(client, **kwargs):
    return AutopilotPass(client, team_id="team1", now=_now, **kwargs)


def test_a_pass_folds_unacted_insights_into_the_batch_it_creates():
    client = CapturingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
        entries=[_entry("e1", "Mornings are the only time the writing happens",
                        categories=["dissertation", "energy"])],
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert result.created_task_ids
    text = client.created_tasks[0]["text"]
    assert "Mornings are the only time the writing happens" in text
    # The person's own tags ride along, and the relevance call is handed to the reader.
    assert "tagged dissertation, energy" in text
    assert "Judge for yourself which of them (if any) bear on the goals above" in text


def test_an_insight_tagged_for_something_else_is_still_shown_not_filtered_out():
    """No tag-to-quest matching anywhere in this code: an insight tagged "home" reaches a quest
    about shipping software, and the model composing the run decides whether it bears on it."""
    client = CapturingClient(
        quests=[_quest("q1", outcome="ship the backend rewrite")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Cut the release")]))},
        entries=[_entry("e1", "Batch the errands into one trip", categories=["home"])],
    )
    _pass_with(client).run({"text": "autopilot pass"})
    assert "Batch the errands into one trip" in client.created_tasks[0]["text"]


def test_only_insights_captured_since_this_quests_last_pass_are_carried():
    client = CapturingClient(
        quests=[_quest("q1", cadence="daily", last_pass_at="2026-07-11T09:00:00Z")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        entries=[_entry("new", "Captured this morning", created_at="2026-07-12T08:00:00Z"),
                 _entry("old", "Captured last week", created_at="2026-07-04T08:00:00Z")],
    )
    _pass_with(client).run({"text": "autopilot pass"})
    text = client.created_tasks[0]["text"]
    assert "Captured this morning" in text
    assert "Captured last week" not in text


def test_a_quest_that_has_never_run_sees_the_whole_window():
    """On a first pass everything recent IS new; showing nothing would be the wrong reading of a
    missing stamp."""
    client = CapturingClient(
        quests=[_quest("q1")],  # no last_pass_at at all
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        entries=[_entry("old", "Captured last week", created_at="2026-07-04T08:00:00Z")],
    )
    _pass_with(client).run({"text": "autopilot pass"})
    assert "Captured last week" in client.created_tasks[0]["text"]


def test_an_acted_on_insight_never_reaches_the_batch():
    client = CapturingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        entries=[_entry("e1", "Already turned into a goal", acted_on=True),
                 _entry("e2", "Still just a note")],
    )
    _pass_with(client).run({"text": "autopilot pass"})
    text = client.created_tasks[0]["text"]
    assert "Still just a note" in text
    assert "Already turned into a goal" not in text


def test_insights_are_read_once_per_pass_and_narrowed_per_quest():
    """They are USER-scoped, so one read serves the pass; but "since the last time it ran" is a
    PER-QUEST question, so the cutoff is applied to that one result in memory."""
    goals = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = CapturingClient(
        quests=[_quest("q1", cadence="daily", last_pass_at="2026-07-11T09:00:00Z"),
                _quest("q2", cadence="daily", last_pass_at="2026-07-01T09:00:00Z")],
        goals_by_quest={"q1": goals, "q2": goals},
        entries=[_entry("new", "Captured this morning", created_at="2026-07-12T08:00:00Z"),
                 _entry("mid", "Captured a week ago", created_at="2026-07-05T08:00:00Z")],
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 2
    assert client.entry_calls == [("coll_1", 0)]        # ONE read for the whole pass
    q1_text, q2_text = client.created_tasks[0]["text"], client.created_tasks[1]["text"]
    assert "Captured this morning" in q1_text and "Captured a week ago" not in q1_text
    assert "Captured this morning" in q2_text and "Captured a week ago" in q2_text


def test_a_pass_with_no_insights_on_record_still_creates_its_batch():
    client = CapturingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
        entries=[],
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "Goal: Draft ch. 2" in text
    assert "insight" not in text.lower()


def test_an_insights_read_that_blows_up_never_fails_the_pass():
    class BrokenInsightsClient(CapturingClient):
        def list_collection_entries(self, collection_id, *, page=0, limit=50):
            raise RuntimeError("Quest entries endpoint down")

    client = BrokenInsightsClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert result.created_task_ids and not result.errors


def test_a_client_without_the_insight_methods_behaves_exactly_as_before():
    client = FakeAutopilotClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1
    assert "Goal: Draft ch. 2" in client.created_tasks[0]["text"]
