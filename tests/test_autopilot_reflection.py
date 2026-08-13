"""Autopilot reads the person's own reflection, and still works when there is none.

The ask that produced this: "does autopilot take into account the latest general reflection?" It
did not — every input to a batch was derived from rows the system recorded (goals, tasks, periods),
so a person could write "the writing goal keeps slipping, protect two mornings" in Quest and the
next pass would compose its brief as if they had said nothing.

Driven against a fake client, offline. The last test is the compatibility one: a client with no
reflection methods at all (every deployment before this, and the fake in ``tests/test_autopilot.py``)
must compose exactly the batch it composed before.
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


class ReflectingClient(FakeAutopilotClient):
    """The autopilot fake plus the two reflection reads, counting how often they are called."""

    def __init__(self, *args, daily=None, reviews=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.daily = dict(daily or {})            # {date-or-None: payload}
        self.reviews = dict(reviews or {})        # {(period, use_previous): review}
        self.daily_calls = []
        self.period_calls = []

    def get_daily_reflection(self, *, date=None):
        self.daily_calls.append(date)
        return dict(self.daily.get(date) or {"has_plan": False})

    def get_period_reflection(self, period, *, use_previous=False, tz=None):
        self.period_calls.append((period, use_previous))
        return dict(self.reviews.get((period, use_previous)) or {"has_review": False})


# --- the composed text ---------------------------------------------------------------------

def test_compose_batch_text_carries_the_reflection_verbatim():
    text = compose_batch_text("ship the thing", [_goal("g1", "Draft chapter two")],
                              reflection="The person's own reflection:\n  Two mornings for writing.")
    assert "Two mornings for writing." in text
    # Named as theirs and pointed at the decision it should influence, not dropped in unlabeled.
    assert "steer which of the above matters most" in text


def test_compose_batch_text_is_unchanged_without_a_reflection():
    goals = [_goal("g1", "Draft chapter two")]
    assert compose_batch_text("ship the thing", goals) == \
        compose_batch_text("ship the thing", goals, reflection=None)


def test_next_steps_artifact_carries_one_condensed_reflection_line():
    steps = next_steps_from_pass([_goal("g1", "Draft chapter two")], scope_label="week:2026_W28",
                                 reflection_note="From their week review: protect two mornings.")
    assert steps.note == "From their week review: protect two mornings."
    # A note, never a step: it explains the list rather than joining it.
    assert steps.steps == ["Draft chapter two"]


# --- the pass ------------------------------------------------------------------------------

def _pass_with(client, **kwargs):
    return AutopilotPass(client, team_id="team1", now=_now, **kwargs)


def test_a_pass_folds_the_daily_reflection_into_the_batch_it_creates():
    client = ReflectingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
        daily={None: {"has_plan": True, "date": "2026-07-12",
                      "yesterday_review": "Lost the afternoon to meetings again."}},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert result.created_task_ids
    assert "Lost the afternoon to meetings again." in client.created_tasks[0]["text"]


def test_a_pass_falls_back_to_the_period_review_when_no_daily_entry_exists():
    client = ReflectingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        reviews={("week", False): {"has_review": True,
                                   "reflection_future": "Protect two mornings for writing."}},
    )
    _pass_with(client).run({"text": "autopilot pass"})
    assert "Protect two mornings for writing." in client.created_tasks[0]["text"]


def test_a_pass_with_no_reflection_on_record_still_creates_its_batch():
    client = ReflectingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1
    assert "Goal: Draft ch. 2" in client.created_tasks[0]["text"]
    assert "reflection" not in client.created_tasks[0]["text"].lower()


def test_the_quest_scope_decides_which_period_review_is_read_first():
    """Nothing here presumes a week matters more than a quarter: a month-scoped quest asks for the
    month review first, and only then the defaults."""
    client = ReflectingClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("month", "2026_07", [_goal("g1")]))},
        reviews={("month", False): {"has_review": True, "reflection_past": "A wide month."}},
    )
    _pass_with(client).run({"text": "autopilot pass"})
    assert client.period_calls[0] == ("month", False)
    assert "A wide month." in client.created_tasks[0]["text"]


def test_reflections_are_read_once_per_pass_not_once_per_quest():
    """They are USER-scoped: re-reading them per quest would be the same text at N times the cost."""
    goals = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = ReflectingClient(
        quests=[_quest("q1"), _quest("q2"), _quest("q3")],
        goals_by_quest={"q1": goals, "q2": goals, "q3": goals},
        daily={None: {"has_plan": True, "date": "2026-07-12",
                      "yesterday_review": "Slow start, good finish."}},
    )
    result = _pass_with(client, daily_budget=5).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 3
    assert client.daily_calls == [None]
    assert all("Slow start, good finish." in t["text"] for t in client.created_tasks)


def test_a_reflection_read_that_blows_up_never_fails_the_pass():
    class BrokenReflectionClient(ReflectingClient):
        def get_daily_reflection(self, *, date=None):
            raise RuntimeError("Quest reflection endpoint down")

        def get_period_reflection(self, period, *, use_previous=False, tz=None):
            raise RuntimeError("Quest reflection endpoint down")

    client = BrokenReflectionClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert result.created_task_ids and not result.errors


def test_a_client_without_the_reflection_methods_behaves_exactly_as_before():
    client = FakeAutopilotClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft ch. 2")]))},
    )
    result = _pass_with(client).run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1
    assert "Goal: Draft ch. 2" in client.created_tasks[0]["text"]
