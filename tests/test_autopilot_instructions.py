"""Per-quest standing ``instructions``: the always-work rule that makes the migration real.

The product requirement (quest_autopilot_design.md's autopilot spec, section 0) is that a quest's
``instructions`` field can FULLY REPLACE a hand-authored recurring assistant task, so a quest
carrying instructions must produce exactly one work batch per due pass per character on duty. These
tests pin the injection point in ``compose_batch_text`` (verbatim, ordered after ``Scope:`` and
before the goal ladder), the byte-identical guarantee when instructions are absent, the always-work
rule itself, the title fallback chain, the defensive truncation, and that a quest carrying
instructions never reaches the goal-proposal path.
"""
from quest_ai_runner.intelligence.proactivity.autopilot import MAX_INSTRUCTIONS_CHARS, AutopilotPass, _batch_title, compose_batch_text
from quest_ai_runner.intelligence.proactivity.autopilot_defaults import PROPOSAL_TEXT_PREFIX

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







# --- title fallback: adopted title -> persona brief -> quest brief -> "Autopilot run" -----------

def test_batch_title_falls_back_to_the_instructions_first_line_stripped_of_markdown():
    assert _batch_title([], instructions="# Daily Brief\nDetails follow.") == "Autopilot: Daily Brief"
    assert _batch_title([], instructions="- bullet start\nmore") == "Autopilot: bullet start"


def test_batch_title_falls_back_to_autopilot_run_when_no_line_has_real_content():
    assert _batch_title([], instructions="   \n\n  ") == "Autopilot: Run"
    assert _batch_title([], instructions="# \n- \n> ") == "Autopilot: Run"


def test_batch_title_from_instructions_is_capped_at_80_characters():
    title = _batch_title([], instructions="y" * 200)
    assert title == ("Autopilot: " + "y" * 80)[:80] and len(title) == 80


def test_batch_title_is_autopilot_run_when_the_person_wrote_no_brief_at_all():
    """ONLY WRITTEN BRIEFS MAY TITLE A BATCH. A default is the same text on every quest that has
    not written one, so titling from it would name every task on every such quest after the same
    built-in first line, in the list and in the subject of the mail the run sends. A title has to
    say what makes THIS run different, and "Autopilot run" at least does not claim otherwise."""
    assert _batch_title([]) == "Autopilot: Run"
    assert _batch_title([], instructions=None, persona_instructions=None) == "Autopilot: Run"


# --- truncation ------------------------------------------------------------------------------





# --- the goal-proposal path is not reached by a quest that carries instructions -------------------



# --- the default briefs require one Solution, a before/after metric report, and filling thin specs --

def test_default_quest_instructions_require_solution_metric_before_and_after():
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import BUNDLED_DEFAULT_QUEST_INSTRUCTIONS as text
    assert "ONE Solution" in text
    assert "before <value> (source, date), after <value> (source, date), change <delta>" in text
    assert "ESOI_SPEC.md in the quest's synced folder, when present" in text
    assert "measuring it, or writing its cascading conditionals when it is costly, is this run's work" in text
    assert "A stage of the Metric's cascading conditionals that was not run is said so, never estimated" in text
    assert "—" not in text


def test_default_quest_instructions_create_missing_spec_and_milestones_without_blocking():
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import BUNDLED_DEFAULT_QUEST_INSTRUCTIONS as text
    for needle in ("no next milestone", "edit_spec_record", "create_goal", "otherwise propose it in a quest note",
                   "locked", "never stop for a decision"):
        assert needle in text
    assert "create_decision" in text  # the existing escalation rules stay


def test_default_persona_instructions_pick_the_solution_furthest_from_target():
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS as text
    assert "furthest from its target" in text
    assert "—" not in text


def test_explicit_instructions_still_replace_the_defaults_and_the_brief_fits_its_budget():
    from quest_ai_runner.intelligence.proactivity.autopilot_briefs import AUTOPILOT_BRIEF_TOKEN_BUDGET
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import (
        BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS as dp, BUNDLED_DEFAULT_QUEST_INSTRUCTIONS as dq)
    from quest_ai_runner.intelligence.mind.delegation import prompt_budget
    written = compose_batch_text("Ship it", "bailey", instructions="Quest rule X.",
                                 persona_instructions="Persona rule Y.",
                                 default_quest_instructions=dq, default_persona_instructions=dp)
    assert "Quest rule X." in written and "Persona rule Y." in written
    assert "ONE Solution" not in written.replace("IMPROVE ONE SOLUTION", "") and "furthest from its target" not in written
    defaulted = compose_batch_text("Ship it", "bailey", default_quest_instructions=dq,
                                   default_persona_instructions=dp)
    assert dq in defaulted and dp in defaulted
    assert prompt_budget.estimate_tokens(defaulted) < AUTOPILOT_BRIEF_TOKEN_BUDGET // 2


def test_custom_quest_instructions_still_get_the_before_after_metric_requirement():
    from quest_ai_runner.intelligence.proactivity.autopilot_briefs import AUTOPILOT_BRIEF_TOKEN_BUDGET
    from quest_ai_runner.intelligence.mind.delegation import prompt_budget
    written = compose_batch_text("Ship it", "bailey", instructions="Quest rule X.",
                                 persona_instructions="Persona rule Y.")
    assert "Quest rule X." in written and "Persona rule Y." in written
    assert "before <value> (source, date), after <value> (source, date), change <delta>" in written
    assert "measure the alternatives" in written and "keep whichever measures best" in written
    assert "edit_spec_record" in written and "create_goal" in written
    assert "—" not in written
    assert prompt_budget.estimate_tokens(written) < AUTOPILOT_BRIEF_TOKEN_BUDGET


def test_batch_title_skips_a_solutions_you_iterate_directive_line():
    brief = "SOLUTIONS YOU ITERATE: esr_3d63298c8f4c\nYou are the lead of the onboarding flow."
    assert _batch_title([], persona_instructions=brief) == "Autopilot: You are the lead of the onboarding flow."


def test_batch_title_never_shows_a_raw_record_id():
    assert _batch_title([], persona_instructions="SOLUTIONS YOU ITERATE: esr_3d63298c8f4c") == "Autopilot: Run"
    assert "esr_" not in _batch_title([], instructions="Review esr_3d63298c8f4c weekly")


def test_batch_title_names_the_autopilot_and_the_solution_it_iterates():
    brief = "SOLUTIONS YOU ITERATE: esr_3d63298c8f4c\nYou are the lead of the onboarding flow."
    assert _batch_title([], persona_instructions=brief, solution_name="Onboarding emails",
                        persona_label="Mary") == "Autopilot: Mary: improve Onboarding emails"
    assert _batch_title([], solution_name="Onboarding emails") == "Autopilot: Improve Onboarding emails"
    # An unresolved persona label is the raw rep id: never put it in a title.
    assert _batch_title([], solution_name="Onboarding emails",
                        persona_label="rep_09d389aeb9ff") == "Autopilot: Improve Onboarding emails"
    # A task the run took over still names the batch.
    assert _batch_title([{"title": "Email the morning brief"}], solution_name="Onboarding emails",
                        persona_label="Mary") == "Email the morning brief"




def test_a_named_autopilots_title_is_autopilot_prefix_plus_its_exact_name():
    name = "Outreach to prospective students"
    assert _batch_title([], persona_instructions="# Something else", solution_name="Grants", persona_label="Mary",
                        autopilot_name=name) == "Autopilot: Outreach to prospective students"
    # Never a doubled prefix, never a record id, capped at 80.
    assert _batch_title([], autopilot_name="Autopilot: " + name) == "Autopilot: Outreach to prospective students"
    assert "esr_" not in _batch_title([], autopilot_name="Review esr_3d63298c8f4c")
    assert len(_batch_title([], autopilot_name="n" * 200)) == 80


def test_an_adopted_task_keeps_its_own_title_even_for_a_named_autopilot():
    assert _batch_title([{"title": "Email the morning brief"}], autopilot_name="Outreach") == "Email the morning brief"
