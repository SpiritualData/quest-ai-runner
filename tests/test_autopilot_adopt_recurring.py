"""Autopilot adopting a quest's own recurring tasks, and the previous-period context it carries.

Two features that only make sense together. Adoption lets ONE pass own everything a quest does in a
period, instead of the pass's batch and the user's recurring tasks executing as unrelated deep runs
that cannot see each other. The previous-period summary is what stops a recurring pass from
starting cold every time and reissuing an instruction the human already fell behind on.

Adoption is opt-in per quest (``autopilot.adopt_recurring``) because it changes who executes a task
the user scheduled themselves.
"""
from datetime import datetime, timezone

import pytest

from quest_ai_runner.runner.autopilot import (
    AutopilotPass,
    compose_batch_text,
    previous_period_bounds,
    previous_period_key,
    select_period_goals,
)

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest

NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)  # a Sunday


def _recurring(task_id, *, text="Send the morning brief", rep=None, scheduled=None,
               status="queued", series="series_1", kind=None):
    t = {"id": task_id, "text": text, "status": status, "goal_id": "q1", "series_id": series}
    if rep:
        t["assignee_rep_id"] = rep
    if scheduled:
        t["scheduled_date"] = scheduled
    if kind:
        t["task_kind"] = kind
    return t


def _passer(client, **kw):
    return AutopilotPass(client, team_id="team1", now=_now, **kw)


# --- the opt-in ------------------------------------------------------------------------------

def test_recurring_tasks_are_left_alone_unless_the_quest_opts_in():
    q1 = _quest("q1")                                    # adopt_recurring absent -> off
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    _passer(client).run({"text": "pass"})
    assert client.task_updates == []                     # the user's task is untouched
    assert "adopted task" not in client.created_tasks[0]["text"]


def test_opted_in_quest_folds_the_recurring_task_into_the_batch_and_closes_it():
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1", text="Email the morning brief")])
    result = _passer(client).run({"text": "pass"})
    assert len(result.created_task_ids) == 1             # ONE run, not two
    created = client.created_tasks[0]
    assert "Email the morning brief" in created["text"]
    assert "adopted task r1" in created["text"]
    # the original occurrence is closed, pointing at the batch that took it over
    assert client.task_updates[0][0] == "r1"
    assert client.task_updates[0][1]["status"] == "done"
    assert created["id"] in client.task_updates[0][1]["result"]


def test_consumer_default_turns_adoption_on_when_the_quest_states_nothing():
    """``adopt_recurring`` is a newer field, so a backend that predates it stores nothing and every
    quest reads as off. Without a consumer-level default there would be no way to enable the
    behavior at all until that backend ships."""
    q1 = _quest("q1")                                    # quest says nothing
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    _passer(client, adopt_recurring_default=True).run({"text": "pass"})
    assert client.task_updates[0][0] == "r1"


def test_an_explicit_quest_setting_beats_the_consumer_default():
    q1 = _quest("q1", adopt_recurring=False)             # the quest opted OUT explicitly
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    _passer(client, adopt_recurring_default=True).run({"text": "pass"})
    assert client.task_updates == []


def test_string_false_does_not_read_as_enabled():
    """A JSON round-trip can leave a boolean as a string; "false" must stay off."""
    q1 = _quest("q1", adopt_recurring="false")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    _passer(client).run({"text": "pass"})
    assert client.task_updates == []


# --- what may and may not be adopted -----------------------------------------------------------

@pytest.mark.parametrize("kind", ["autopilot", "autopilot_work"])
def test_autopilots_own_tasks_are_never_adopted(kind):
    """Adopting the recurring PASS task would fold the scanner into its own batch and close it,
    killing the series that drives autopilot at all. Adopting a previous work batch would let a
    pass swallow its own output. Neither is recoverable from inside a pass."""
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1", kind=kind)])
    _passer(client).run({"text": "pass"})
    assert client.task_updates == []


def test_one_off_tasks_are_not_adopted():
    """A task the user queued once is not a recurring series and is not autopilot's to take over."""
    q1 = _quest("q1", adopt_recurring=True)
    one_off = {"id": "t1", "text": "one off", "status": "queued", "goal_id": "q1"}
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[one_off])
    _passer(client).run({"text": "pass"})
    assert client.task_updates == []


def test_a_future_dated_occurrence_is_not_yet_due():
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1", scheduled="2026-07-20")])
    _passer(client).run({"text": "pass"})
    assert client.task_updates == []


def test_adopted_task_alone_still_produces_a_batch_when_no_goal_is_eligible():
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", []))},
        tasks=[_recurring("r1", text="Email the morning brief")])
    result = _passer(client).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert "Email the morning brief" in client.created_tasks[0]["text"]


# --- persona routing of adopted tasks ----------------------------------------------------------

def test_an_adopted_task_keeps_its_own_persona_and_gets_its_own_batch():
    q1 = _quest("q1", adopt_recurring=True,
                personas=[{"rep_id": "rep_bailey"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1", rep="rep_batman")])
    result = _passer(client).run({"text": "pass"})
    assert len(result.created_task_ids) == 2
    by_rep = {t.get("assignee_rep_id"): t for t in client.created_tasks}
    assert "adopted task r1" in by_rep["rep_batman"]["text"]
    assert "adopted task r1" not in by_rep["rep_bailey"]["text"]


def test_an_unassigned_adopted_task_rides_along_with_todays_persona():
    """Otherwise the same character would get two separate deep runs for the same period, which is
    exactly the duplication adoption exists to remove."""
    q1 = _quest("q1", adopt_recurring=True, personas=[{"rep_id": "rep_bailey"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    result = _passer(client).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert client.created_tasks[0]["assignee_rep_id"] == "rep_bailey"
    assert "adopted task r1" in client.created_tasks[0]["text"]


# --- failure direction --------------------------------------------------------------------------

def test_a_failed_close_duplicates_work_rather_than_losing_it():
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    client.update_task_error = RuntimeError("500")
    result = _passer(client).run({"text": "pass"})
    assert len(result.created_task_ids) == 1             # the batch still exists
    assert result.bookkeeping_warnings                    # and the failure is reported, not silent
    assert "duplicated work, not missing work" in result.bookkeeping_warnings[0]["detail"]


def test_dry_run_adopts_nothing():
    q1 = _quest("q1", adopt_recurring=True)
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        tasks=[_recurring("r1")])
    result = _passer(client).run({"text": "autopilot dry-run"})
    assert client.created_tasks == []
    assert client.task_updates == []
    assert result.proposals[0]["adopted_task_ids"] == ["r1"]


# --- previous-period context ---------------------------------------------------------------------

@pytest.mark.parametrize("scope,expected", [
    ("day", "2026-07-11"),
    ("week", "2026_W27"),        # NOW is Sunday 7/12, the last day of W28
    ("month", "2026_06"),
    ("quarter", "2026_Q2"),
    ("year", "2025"),
])
def test_previous_period_key_steps_back_one_period(scope, expected):
    assert previous_period_key(scope, NOW) == expected


def test_previous_period_bounds_are_half_open_and_cover_exactly_one_period():
    start, end = previous_period_bounds("day", NOW)
    assert start.isoformat().startswith("2026-07-11T00:00")
    assert end.isoformat().startswith("2026-07-12T00:00")


def test_batch_text_reports_the_previous_period_goals_and_task_results():
    text = compose_batch_text(
        "ship it", [_goal("g1", name="Today's goal")], "rep_bailey",
        scope_label="day:2026-07-12",
        previous={"period": "day:2026-07-11",
                  "goals": [_goal("g0", name="Yesterday done", completed=True),
                            _goal("g9", name="Yesterday missed")],
                  "tasks": [{"status": "done", "title": "Brief", "result": "sent the brief"}]})
    assert "day:2026-07-12" in text
    assert "Goals completed: Yesterday done" in text
    assert "Yesterday missed" in text
    assert "do not silently drop" in text
    assert "sent the brief" in text


def test_batch_text_says_so_when_the_previous_period_produced_nothing():
    """Silence must be stated, not omitted: an absent section reads as no information, while an
    explicit 'no recorded activity' is the signal that the plan may need re-sequencing."""
    text = compose_batch_text("ship it", [_goal("g1")], None,
                              previous={"period": "day:2026-07-11", "goals": [], "tasks": []})
    assert "No recorded activity" in text


def test_previous_period_summary_is_attached_to_a_real_pass():
    q1 = _quest("q1", adopt_recurring=True)
    payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]),
                             ("day", "2026-07-11", [_goal("g0", name="Yesterday", completed=True)]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": payload})
    _passer(client).run({"text": "pass"})
    assert "Goals completed: Yesterday" in client.created_tasks[0]["text"]


def test_select_period_goals_returns_completed_goals_too():
    """The current-scope selector filters completed goals out (they are not work). The previous
    period needs them precisely BECAUSE they are done: that is the progress being reported."""
    payload = _goals_payload(("day", "2026-07-11", [_goal("g0", completed=True)]))
    assert len(select_period_goals(payload, "day", "2026-07-11")) == 1
