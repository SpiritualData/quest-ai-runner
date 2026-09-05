"""The goal ladder: an autopilot run sees the person's CURRENT goals at every horizon.

The ladder is the ONLY way a goal reaches a run. Goals are context for autopilot and never an
assignment to it: nothing selects one, hands one to a character, or composes one as an instruction.
What a run produces is decided by its persona's standing instructions, and the ladder is what that
output has to add up to.

These tests pin the parts that are product decisions rather than implementation details:

  * every current goal is on the ladder, including the ones the person is plainly doing themselves,
    while none of them ever becomes work;
  * only ``completed`` excludes a goal, a horizon with nothing current is omitted entirely, and an
    empty ladder composes to nothing at all;
  * each horizon is capped, nearest deadline first, and says how many it left out;
  * a run working standing instructions carries the ladder, which is the case that motivated the
    feature: without it, that run carries no goal information whatsoever;
  * omitting the parameter is byte-identical to passing None, the same guarantee ``instructions``
    and ``persona_instructions`` carry.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    DEFAULT_LADDER_PER_SCOPE,
    AutopilotPass,
    compose_batch_text,
    current_goal_ladder,
    current_scope_label,
)

from .test_autopilot import NOW, FakeAutopilotClient, _goal, _goals_payload, _now, _quest

# NOW is 2026-07-12 (a Sunday): day "2026-07-12", week "2026_W28", month "2026_07",
# quarter "2026_Q3", year "2026" -- the backend's own period id formats.
FULL_LADDER_PAYLOAD = _goals_payload(
    ("day", "2026-07-12", [_goal("d1", "Draft the rubric")]),
    ("week", "2026_W28", [_goal("w1", "Finish chapter two")]),
    ("month", "2026_07", [_goal("m1", "Submit the chapter")]),
    ("quarter", "2026_Q3", [_goal("q1", "Pass the committee review")]),
    ("year", "2026", [_goal("y1", "Have the PhD")]),
)


def _names(ladder, scope):
    for rung in ladder:
        if rung["scope"] == scope:
            return [g["name"] for g in rung["goals"]]
    return None


# --- every current horizon, the run's own scope included -----------------------------------------

def test_the_ladder_covers_every_current_horizon_from_day_up_to_year():
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW)
    assert [rung["scope"] for rung in ladder] == ["day", "week", "month", "quarter", "year"]
    assert [rung["period"] for rung in ladder] == [
        "2026-07-12", "2026_W28", "2026_07", "2026_Q3", "2026"]
    assert _names(ladder, "year") == ["Have the PhD"]


def test_the_scope_the_pass_names_is_on_the_ladder_like_every_other_horizon():
    """The scope label says which period the quest is planning in; the ladder shows that period's
    goals alongside the ones above it, so the run sees the whole shape rather than one slice."""
    assert current_scope_label(FULL_LADDER_PAYLOAD, NOW) == "day:2026-07-12"
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW)
    assert _names(ladder, "day") == ["Draft the rubric"]
    assert len(ladder) == 5


def test_a_period_that_is_not_current_is_never_on_the_ladder():
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "Today's goal")]),
        ("day", "2026-07-11", [_goal("d0", "Yesterday's goal")]),
        ("month", "2026_06", [_goal("m0", "Last month's goal")]),
        ("month", "2026_07", [_goal("m1", "This month's goal")]),
    )
    ladder = current_goal_ladder(payload, NOW)
    assert _names(ladder, "day") == ["Today's goal"]
    assert _names(ladder, "month") == ["This month's goal"]


# --- context, never work -------------------------------------------------------------------------

def test_a_goal_the_person_is_doing_themselves_is_on_the_ladder_by_design():
    """Nothing distinguishes goals here, and that is the point. A goal the AI will never touch is
    still what its work has to add up to, so it is context the run must see."""
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "Rewrite the rubric"),
                               _goal("d2", "I call the committee myself")]),
        ("month", "2026_07", [_goal("m1", "I defend in person")]),
    )
    ladder = current_goal_ladder(payload, NOW)
    assert _names(ladder, "day") == ["Rewrite the rubric", "I call the committee myself"]
    assert _names(ladder, "month") == ["I defend in person"]


def test_no_goal_is_ever_composed_as_an_instruction():
    """The whole product decision in one assertion: a goal is named as context and never appears
    as something this run has been handed."""
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "Rewrite the rubric"),
                               _goal("d2", "I call the committee myself")]))
    text = compose_batch_text("Get the PhD", scope_label="day:2026-07-12",
                              default_instructions="Work this quest today.",
                              goal_ladder=current_goal_ladder(payload, NOW))
    assert "- Rewrite the rubric" in text
    assert "- I call the committee myself" in text
    assert "Goal: " not in text
    assert "Done when:" not in text
    assert "This run does not own these goals" in text


# --- exclusions and emptiness ------------------------------------------------------------------

def test_completed_goals_are_excluded_and_an_emptied_horizon_is_omitted():
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "Still open"), _goal("d2", "Already done",
                                                                completed=True)]),
        ("week", "2026_W28", [_goal("w1", "Also done", completed=True)]),
        ("month", "2026_07", [_goal("m1", "Open too")]),
    )
    ladder = current_goal_ladder(payload, NOW)
    assert [rung["scope"] for rung in ladder] == ["day", "month"]  # the week rung is gone entirely
    assert _names(ladder, "day") == ["Still open"]


def test_no_current_horizon_at_all_yields_an_empty_ladder():
    payload = _goals_payload(("month", "2026_03", [_goal("m1", "An old goal")]))
    assert current_goal_ladder(payload, NOW) == []
    assert current_goal_ladder({}, NOW) == []


def test_an_empty_ladder_emits_nothing_and_composes_like_no_ladder_at_all():
    assert (compose_batch_text("Ship it", goal_ladder=[])
            == compose_batch_text("Ship it"))


# --- the per-horizon cap ------------------------------------------------------------------------

def test_a_horizon_is_capped_nearest_deadline_first_and_says_how_many_it_left_out():
    """A real quest carries around twenty goals in one month, so an uncapped dump would swamp the
    prompt it is meant to orient. What is left out is stated, never silently dropped."""
    month_goals = [_goal(f"m{i}", f"Goal {i}", deadline=f"2026-07-{i:02d}")
                   for i in range(20, 8, -1)]  # payload order is deliberately deadline-DESCENDING
    payload = _goals_payload(("month", "2026_07", month_goals))
    ladder = current_goal_ladder(payload, NOW, per_scope_limit=3)
    assert _names(ladder, "month") == ["Goal 9", "Goal 10", "Goal 11"]
    assert ladder[0]["more"] == 9
    assert "+9 more" in compose_batch_text("Ship it", goal_ladder=ladder)


def test_undated_goals_sort_after_dated_ones_and_keep_the_payload_order():
    payload = _goals_payload(("month", "2026_07", [
        _goal("m1", "No date, first in the plan"),
        _goal("m2", "Due late", deadline="2026-07-30"),
        _goal("m3", "No date, second in the plan"),
        _goal("m4", "Due soon", deadline="2026-07-14"),
    ]))
    ladder = current_goal_ladder(payload, NOW)
    assert _names(ladder, "month") == ["Due soon", "Due late",
                                       "No date, first in the plan", "No date, second in the plan"]


def test_the_default_cap_is_the_module_constant():
    goals = [_goal(f"m{i}", f"Goal {i}") for i in range(DEFAULT_LADDER_PER_SCOPE + 5)]
    ladder = current_goal_ladder(_goals_payload(("month", "2026_07", goals)), NOW)
    assert len(ladder[0]["goals"]) == DEFAULT_LADDER_PER_SCOPE
    assert ladder[0]["more"] == 5


# --- placement, framing and the byte-identical guarantee -----------------------------------------

def test_the_ladder_sits_after_the_brief_and_says_it_is_not_this_runs_work():
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW)
    text = compose_batch_text("Get the PhD", "bailey", scope_label="day:2026-07-12",
                              instructions="Write it up.", goal_ladder=ladder)
    assert text.index("Standing instructions for this quest") < text.index(
        "THE PERSON'S CURRENT GOALS")
    assert "This run does not own these goals" in text
    assert "adds up to the horizons above them" in text


def test_a_deadline_is_shown_when_a_ladder_goal_has_one():
    payload = _goals_payload(("month", "2026_07", [_goal("m1", "Submit", deadline="2026-07-31")]))
    text = compose_batch_text("Ship it", goal_ladder=current_goal_ladder(payload, NOW))
    assert "- Submit (by 2026-07-31)" in text


def test_omitting_the_ladder_is_byte_identical_to_passing_none():
    kwargs = dict(scope_label="day:2026-07-12", instructions="Do the thing.",
                  reflection="what I said", insights="what I noted")
    omitted = compose_batch_text("Ship it", "bailey", **kwargs)
    explicit_none = compose_batch_text("Ship it", "bailey", goal_ladder=None, **kwargs)
    assert omitted == explicit_none
    assert "THE PERSON'S CURRENT GOALS" not in omitted


# --- end to end through a pass -------------------------------------------------------------------

def test_a_created_batch_task_carries_the_whole_ladder_and_no_goal_blocks():
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": FULL_LADDER_PAYLOAD})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "THE PERSON'S CURRENT GOALS" in text
    for name in ("Draft the rubric", "Finish chapter two", "Submit the chapter",
                 "Pass the committee review", "Have the PhD"):
        assert f"- {name}" in text
        assert f"Goal: {name}" not in text   # context only, at every horizon


def test_an_instructions_run_carries_the_ladder_even_though_no_goal_is_its_work():
    """The case the ladder exists for. This quest's daily brief runs on standing instructions, so
    without the ladder it would be the run carrying NO goal information at all, while being the run
    most in need of direction."""
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    q1["autopilot"]["instructions"] = "Write the daily brief."
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "I call the committee myself")]),
        ("quarter", "2026_Q3", [_goal("q1g", "Pass the committee review")]),
    )
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": payload})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "Goal: " not in text                       # a standing-instructions run, no work blocks
    assert "Write the daily brief." in text
    assert "I call the committee myself" in text      # ...and yet it knows what it is aiming at
    assert "Pass the committee review" in text


def test_a_quest_with_no_current_goals_composes_without_a_ladder():
    """No current horizon means no ladder, and a batch that carries none must be untouched."""
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    q1["autopilot"]["instructions"] = "Write the daily brief."
    payload = _goals_payload(("month", "2026_03", [_goal("old", "An old goal")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": payload})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    assert "THE PERSON'S CURRENT GOALS" not in client.created_tasks[0]["text"]


def test_the_ladder_costs_no_extra_api_call():
    """It is built from the goals payload the pass already fetched. One read per quest, as before."""
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": FULL_LADDER_PAYLOAD})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert client.goal_list_calls == ["q1"]


def test_the_ladder_follows_the_clock_not_a_fixed_payload_shape():
    """A pass running in a different month reads a different rung from the same payload, which is
    what proves the ladder shares ``current_scope_label``'s period matcher rather than a second one
    that could drift from it."""
    payload = _goals_payload(("month", "2026_07", [_goal("m1", "July's goal")]),
                             ("month", "2026_08", [_goal("m2", "August's goal")]))
    august = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
    assert _names(current_goal_ladder(payload, NOW), "month") == ["July's goal"]
    assert _names(current_goal_ladder(payload, august), "month") == ["August's goal"]
    assert current_scope_label(payload, august) == "month:2026_08"
