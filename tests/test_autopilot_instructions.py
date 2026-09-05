"""Per-quest standing ``instructions``: the always-work rule that makes the migration real.

The product requirement (quest_autopilot_design.md's autopilot spec, section 0) is that a quest's
``instructions`` field can FULLY REPLACE a hand-authored recurring assistant task, so a quest
carrying instructions must produce exactly one work batch per due pass per character on duty. These
tests pin the injection point in ``compose_batch_text`` (verbatim, ordered after ``Scope:`` and
before the goal ladder), the byte-identical guarantee when instructions are absent, the always-work
rule itself, the title fallback chain, the defensive truncation, and that a quest carrying
instructions never reaches the goal-proposal path.
"""
from quest_ai_runner.runner.autopilot import (
    MAX_INSTRUCTIONS_CHARS,
    PROPOSAL_TEXT_PREFIX,
    AutopilotPass,
    _batch_title,
    compose_batch_text,
)

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest


# --- injection point and verbatim composition (compose_batch_text) -------------------------------

def test_instructions_placed_after_scope_and_before_the_goal_ladder_verbatim():
    instructions = "Write a short daily update.\n\nInclude yesterday's numbers, verbatim."
    ladder = [{"scope": "day", "period": "2026-07-12",
               "goals": [{"name": "Draft the rubric", "deadline": ""}], "more": 0}]
    text = compose_batch_text("Ship the launch", scope_label="day:2026-07-12",
                              instructions=instructions, goal_ladder=ladder)
    scope_idx = text.index("Scope: this quest's day:2026-07-12")
    instructions_idx = text.index(instructions)   # exact substring -- never reflowed/rewritten
    ladder_idx = text.index("THE PERSON'S CURRENT GOALS")
    assert scope_idx < instructions_idx < ladder_idx
    assert "Standing instructions for this quest, written by the person who owns it" in text


def test_no_instructions_is_byte_identical_to_before_the_parameter_existed():
    kwargs = dict(scope_label="day:2026-07-12", reflection="what I said", insights="what I noted")
    omitted = compose_batch_text("Ship it", "bailey", **kwargs)
    explicit_none = compose_batch_text("Ship it", "bailey", instructions=None, **kwargs)
    assert omitted == explicit_none
    assert "Standing instructions" not in omitted
    # The same guarantee for the per-persona field that landed alongside it: neither instructions
    # parameter may cost a single byte on the composition of a quest that uses neither.
    both_none = compose_batch_text("Ship it", "bailey", instructions=None,
                                   persona_instructions=None, **kwargs)
    assert both_none == omitted


# --- the always-work rule (AutopilotPass) ---------------------------------------------------------

def test_instructions_only_creates_exactly_one_batch_task_carrying_the_instructions():
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["instructions"] = "Produce a short daily brief and send it."
    # No goals at all for this quest -- goals are not what makes a pass work.
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    passer = AutopilotPass(client, team_id="team1", daily_budget=3, now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created_text = client.created_tasks[0]["text"]
    assert "Produce a short daily brief and send it." in created_text


def test_instructions_only_consumes_exactly_one_budget_unit():
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["instructions"] = "Do the first quest's daily thing."
    q2 = _quest("q2", mode="act")
    q2["autopilot"]["instructions"] = "Do the second quest's daily thing."
    client = FakeAutopilotClient(quests=[q1, q2], goals_by_quest={})
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert any(s["quest_id"] == "q2" and "budget" in s["reason"] for s in result.skipped)


def test_the_quests_instructions_ride_into_a_batch_that_also_has_a_goal_ladder():
    """The two are not alternatives. The brief says what to produce; the ladder says what that has
    to add up to, and a quest with both carries both."""
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["instructions"] = "Produce a short daily brief and send it."
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Produce a short daily brief and send it." in text
    assert "- Draft chapter three" in text
    assert "Goal: Draft chapter three" not in text


# --- title fallback: adopted title -> persona brief -> quest brief -> default brief ---------------

def test_batch_title_falls_back_to_the_instructions_first_line_stripped_of_markdown():
    assert _batch_title([], instructions="# Daily Brief\nDetails follow.") == "Daily Brief"
    assert _batch_title([], instructions="- bullet start\nmore") == "bullet start"


def test_batch_title_falls_back_to_autopilot_run_when_no_line_has_real_content():
    assert _batch_title([], instructions="   \n\n  ") == "Autopilot run"
    assert _batch_title([], instructions="# \n- \n> ") == "Autopilot run"


def test_batch_title_from_instructions_is_capped_at_80_characters():
    title = _batch_title([], instructions="y" * 200)
    assert title == "y" * 80


def test_batch_title_falls_through_to_the_default_brief_when_nobody_wrote_one():
    """An unconfigured quest is still titled after what its run will do, rather than after the
    "Act as ..." line the server would otherwise derive a title from."""
    assert _batch_title([], default_instructions="Work this quest today.\nMore.") \
        == "Work this quest today."
    assert _batch_title([], instructions="The quest's own brief",
                        default_instructions="Work this quest today.") == "The quest's own brief"


def test_batch_title_none_when_nothing_at_all_is_given():
    assert _batch_title([], instructions=None) is None


# --- truncation ------------------------------------------------------------------------------

def test_over_cap_instructions_are_truncated_and_a_warning_is_logged(caplog):
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["instructions"] = "x" * (MAX_INSTRUCTIONS_CHARS + 100)
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    with caplog.at_level("WARNING"):
        result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created_text = client.created_tasks[0]["text"]
    assert ("x" * MAX_INSTRUCTIONS_CHARS) in created_text
    assert ("x" * (MAX_INSTRUCTIONS_CHARS + 1)) not in created_text
    assert f"truncated from {MAX_INSTRUCTIONS_CHARS + 100} to {MAX_INSTRUCTIONS_CHARS}" in caplog.text


def test_exactly_at_cap_instructions_are_not_touched():
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["instructions"] = "z" * MAX_INSTRUCTIONS_CHARS
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert ("z" * MAX_INSTRUCTIONS_CHARS) in client.created_tasks[0]["text"]


# --- the goal-proposal path is not reached by a quest that carries instructions -------------------

def test_instructions_set_with_plan_and_work_produces_no_goal_proposal():
    q1 = _quest("q1", mode="act", planning="plan_and_work")
    q1["autopilot"]["instructions"] = "Produce a short update, no proposals needed."
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created_text = client.created_tasks[0]["text"]
    assert not created_text.startswith(PROPOSAL_TEXT_PREFIX)
    assert not any(c.get("kind") == "goal_proposal" for c in result.created)
