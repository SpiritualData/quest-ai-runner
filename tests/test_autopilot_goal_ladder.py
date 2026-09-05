"""The goal ladder: an autopilot run sees the person's CURRENT goals at every horizon.

``select_target_goals`` picks exactly ONE scope (finest first) and hands the run only that scope's
``ai_help`` goals. Everything above it is invisible, so a run advancing today's goal has no idea
which month or quarter that day is meant to add up to. ``current_goal_ladder`` is the missing half:
day, week, month, quarter and year, as CONTEXT rather than as this run's work.

These tests pin the parts that are product decisions rather than implementation details, because
each is the opposite of what target selection does and would otherwise read as a bug:

  * goals WITHOUT ``ai_help`` are on the ladder (a goal the person kept for themselves is still
    what the AI's work must add up to) while never becoming a ``Goal:`` work block;
  * only ``completed`` excludes a goal, a horizon with nothing current is omitted entirely, and an
    empty ladder composes to nothing at all;
  * each horizon is capped, nearest deadline first, and says how many it left out;
  * this run's own targets are marked, so the reader can tell the slice from the frame;
  * an instructions-only batch (no goal blocks at all) carries the ladder, which is the case that
    motivated the feature: without it, that run carries no goal information whatsoever;
  * omitting the parameter is byte-identical to passing None, the same guarantee ``instructions``
    and ``persona_instructions`` carry.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    DEFAULT_LADDER_PER_SCOPE,
    AutopilotPass,
    compose_batch_text,
    current_goal_ladder,
    select_target_goals,
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


# --- every current horizon, including the run's own ---------------------------------------------

def test_the_ladder_covers_every_current_horizon_from_day_up_to_year():
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW)
    assert [rung["scope"] for rung in ladder] == ["day", "week", "month", "quarter", "year"]
    assert [rung["period"] for rung in ladder] == [
        "2026-07-12", "2026_W28", "2026_07", "2026_Q3", "2026"]
    assert _names(ladder, "year") == ["Have the PhD"]


def test_the_runs_own_scope_is_on_the_ladder_as_well_as_the_horizons_above_it():
    """The run works the day; the ladder still shows the day, because dropping it would leave the
    marked-target line with nothing to mark and make the run's own slice the one thing missing from
    the picture of what it serves."""
    targets, scope_label = select_target_goals(FULL_LADDER_PAYLOAD, NOW)
    assert scope_label == "day:2026-07-12"
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW,
                                 target_goal_ids=[g["id"] for g in targets])
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


# --- the ai_help decision: on the ladder, never a work block -------------------------------------

def test_goals_without_ai_help_are_on_the_ladder_by_design():
    """The point of the feature. A human-only goal is still what the AI's work has to add up to,
    so it is context the run must see, even though the run may not work it."""
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "AI drafts the rubric", ai_help=True),
                               _goal("d2", "I call the committee myself", ai_help=False)]),
        ("month", "2026_07", [_goal("m1", "I defend in person", ai_help=False)]),
    )
    ladder = current_goal_ladder(payload, NOW)
    assert _names(ladder, "day") == ["AI drafts the rubric", "I call the committee myself"]
    assert _names(ladder, "month") == ["I defend in person"]


def test_a_human_only_goal_on_the_ladder_never_becomes_a_goal_work_block():
    """Same goal, two very different roles: named in the ladder as context, and absent from the
    ``Goal:`` blocks, which are the run's actual assignment."""
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("d1", "AI drafts the rubric", ai_help=True),
                               _goal("d2", "I call the committee myself", ai_help=False)]))
    targets, _scope = select_target_goals(payload, NOW)
    assert [g["id"] for g in targets] == ["d1"]     # only the ai_help goal is worked
    text = compose_batch_text("Get the PhD", targets, scope_label="day:2026-07-12",
                              goal_ladder=current_goal_ladder(
                                  payload, NOW, target_goal_ids=["d1"]))
    assert "I call the committee myself" in text            # present as context
    assert "Goal: I call the committee myself" not in text  # never as work
    assert "Goal: AI drafts the rubric" in text


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
    goals = [{"id": "g1", "name": "A goal", "description": "brief"}]
    assert (compose_batch_text("Ship it", goals, goal_ladder=[])
            == compose_batch_text("Ship it", goals))


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
    assert "+9 more" in compose_batch_text("Ship it", [], goal_ladder=ladder)


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


# --- marking this run's own targets ---------------------------------------------------------------

def test_this_runs_targets_are_marked_and_nothing_else_is():
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW, target_goal_ids=["d1"])
    marked = {g["name"] for rung in ladder for g in rung["goals"] if g["is_target"]}
    assert marked == {"Draft the rubric"}
    text = compose_batch_text("Get the PhD", [], goal_ladder=ladder)
    assert "- Draft the rubric [this run]" in text
    assert "- Have the PhD [this run]" not in text
    assert "- Have the PhD" in text


def test_no_target_ids_means_nothing_is_marked():
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW)
    assert not any(g["is_target"] for rung in ladder for g in rung["goals"])


# --- placement, framing and the byte-identical guarantee -----------------------------------------

def test_the_ladder_sits_before_the_first_goal_block_and_says_it_is_not_this_runs_work():
    goals = [{"id": "d1", "name": "Draft the rubric", "description": "brief"}]
    ladder = current_goal_ladder(FULL_LADDER_PAYLOAD, NOW, target_goal_ids=["d1"])
    text = compose_batch_text("Get the PhD", goals, "bailey", scope_label="day:2026-07-12",
                              instructions="Write it up.", goal_ladder=ladder)
    assert text.index("THE PERSON'S CURRENT GOALS") < text.index("Goal: Draft the rubric")
    assert text.index("Standing instructions for this quest") < text.index(
        "THE PERSON'S CURRENT GOALS")
    assert "This run does not own these goals" in text
    assert "adds up to the horizons above them" in text


def test_a_deadline_is_shown_when_a_ladder_goal_has_one():
    payload = _goals_payload(("month", "2026_07", [_goal("m1", "Submit", deadline="2026-07-31")]))
    text = compose_batch_text("Ship it", [], goal_ladder=current_goal_ladder(payload, NOW))
    assert "- Submit (by 2026-07-31)" in text


def test_omitting_the_ladder_is_byte_identical_to_passing_none():
    goals = [{"id": "g1", "name": "A goal", "description": "brief"}]
    kwargs = dict(scope_label="day:2026-07-12", instructions="Do the thing.",
                  reflection="what I said", insights="what I noted")
    omitted = compose_batch_text("Ship it", goals, "bailey", **kwargs)
    explicit_none = compose_batch_text("Ship it", goals, "bailey", goal_ladder=None, **kwargs)
    assert omitted == explicit_none
    assert "THE PERSON'S CURRENT GOALS" not in omitted


# --- end to end through a pass -------------------------------------------------------------------

def test_a_created_batch_task_carries_the_ladder_with_its_targets_marked():
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": FULL_LADDER_PAYLOAD})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "THE PERSON'S CURRENT GOALS" in text
    assert "- Draft the rubric [this run]" in text
    for name in ("Finish chapter two", "Submit the chapter", "Pass the committee review",
                 "Have the PhD"):
        assert name in text
        assert f"Goal: {name}" not in text   # context only; the day goal is the only work block


def test_an_instructions_only_run_carries_the_ladder_even_with_no_goal_blocks():
    """The case the ladder exists for. This quest's daily brief and its Saturday review both run on
    standing instructions with no eligible goal, so without the ladder they are the runs carrying
    NO goal information at all, while being the runs most in need of direction."""
    q1 = _quest("q1", mode="act", outcome="Get the PhD")
    q1["autopilot"]["instructions"] = "Write the daily brief."
    payload = _goals_payload(
        # Nothing here is ai_help, so no goal is eligible and no Goal: block is composed.
        ("day", "2026-07-12", [_goal("d1", "I call the committee myself", ai_help=False)]),
        ("quarter", "2026_Q3", [_goal("q1g", "Pass the committee review", ai_help=False)]),
    )
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": payload})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "Goal: " not in text                       # a standing-instructions run, no work blocks
    assert "Write the daily brief." in text
    assert "I call the committee myself" in text      # ...and yet it knows what it is aiming at
    assert "Pass the committee review" in text
    # This run owns none of them, so no ladder LINE is marked (the framing paragraph explains the
    # marker either way, which is why the check is on the lines rather than the whole text).
    assert not any(line.startswith("  - ") and "[this run]" in line
                   for line in text.splitlines())


def test_a_quest_with_no_current_goals_composes_exactly_as_before():
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
    what proves the ladder shares ``select_target_goals``' period matcher rather than a second one
    that could drift from it."""
    payload = _goals_payload(("month", "2026_07", [_goal("m1", "July's goal")]),
                             ("month", "2026_08", [_goal("m2", "August's goal")]))
    august = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
    assert _names(current_goal_ladder(payload, NOW), "month") == ["July's goal"]
    assert _names(current_goal_ladder(payload, august), "month") == ["August's goal"]
