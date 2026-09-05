"""Per-PERSONA standing instructions: one roster, two characters, two different jobs.

A quest's roster can hold characters who do genuinely unrelated work on it: the one who advances
the goals most days, and the specialist rostered for one day to look at the quest from outside and
propose decisions about it. Two things break that before this feature. Every character gets the
identical quest-wide brief, which describes neither job well. And ``resolve_persona``'s
day-matched-beats-unrestricted precedence hands the specialist ALL of that day's goals, so the
weekday worker goes quiet on the one day the specialist is around.

These tests pin the fix: a roster entry's own ``instructions`` (injected after the quest-wide block
and before the goal ladder, verbatim), its ``instructions_only`` flag (invisible to work routing,
still on duty and still getting its own batch), one batch per character on duty, and the title
fallback. The rule that a character works only on their own days lives in
``test_autopilot_day_authority.py``.
"""
from quest_ai_runner.runner.autopilot import (
    MAX_INSTRUCTIONS_CHARS,
    AutopilotPass,
    _batch_title,
    compose_batch_text,
    persona_entries_on_duty,
    persona_instructions_for,
    personas_on_duty,
    resolve_persona,
    resolve_task_persona,
)

from .test_autopilot import NOW, FakeAutopilotClient, _goal, _goals_payload, _now, _quest

# NOW is a Sunday, so "Sun" is today and "Sat" is a day this pass is NOT on.
TODAY = ["Sun"]
NOT_TODAY = ["Sat"]

BAILEY_BRIEF = "Advance the dissertation chapter in front of you and say what is left."
BATMAN_BRIEF = "Review how the funding work and the dissertation relate, and propose one decision."


def _roster_quest(quest_id="q1", *, personas, mode="act", planning="work_only",
                  instructions=None):
    quest = _quest(quest_id, mode=mode, planning=planning, personas=personas)
    if instructions is not None:
        quest["autopilot"]["instructions"] = instructions
    return quest


# --- injection point and verbatim composition (compose_batch_text) -------------------------------

def test_persona_instructions_sit_between_the_quest_block_and_the_goal_ladder_verbatim():
    ladder = [{"scope": "day", "period": "2026-07-12",
               "goals": [{"name": "A goal", "deadline": ""}], "more": 0}]
    text = compose_batch_text("Ship the launch", "Batman", scope_label="day:2026-07-12",
                              instructions=BAILEY_BRIEF, persona_instructions=BATMAN_BRIEF,
                              goal_ladder=ladder)
    scope_idx = text.index("Scope: this quest's day:2026-07-12")
    quest_idx = text.index(BAILEY_BRIEF)          # exact substrings -- never reflowed/rewritten
    persona_idx = text.index(BATMAN_BRIEF)
    ladder_idx = text.index("THE PERSON'S CURRENT GOALS")
    assert scope_idx < quest_idx < persona_idx < ladder_idx


def test_persona_preamble_names_the_persona_and_states_which_instructions_win():
    text = compose_batch_text("Ship it", "Batman", persona_instructions=BATMAN_BRIEF)
    assert "Standing instructions for Batman specifically on this quest" in text
    assert "MORE SPECIFIC" in text


def test_persona_preamble_still_reads_when_no_persona_was_resolved():
    text = compose_batch_text("Ship it", persona_instructions=BATMAN_BRIEF)
    assert "Standing instructions for this character specifically on this quest" in text
    assert BATMAN_BRIEF in text


def test_no_persona_instructions_is_byte_identical_to_before_the_parameter_existed():
    kwargs = dict(scope_label="day:2026-07-12", reflection="what I said",
                  instructions="the quest's own brief")
    omitted = compose_batch_text("Ship it", "bailey", **kwargs)
    explicit_none = compose_batch_text("Ship it", "bailey",
                                       persona_instructions=None, **kwargs)
    assert omitted == explicit_none
    assert "specifically on this quest" not in omitted


# --- the roster reader: entries on duty, and a character's own instructions -----------------------

def test_persona_entries_on_duty_returns_whole_entries_day_matched_first():
    cfg = {"personas": [
        {"rep_id": "rep_anyone", "instructions": BAILEY_BRIEF},
        {"rep_id": "rep_sunday", "days": TODAY, "instructions": BATMAN_BRIEF},
    ]}
    entries = persona_entries_on_duty(cfg, NOW)
    assert [e["rep_id"] for e in entries] == ["rep_sunday", "rep_anyone"]
    assert entries[0]["instructions"] == BATMAN_BRIEF


def test_persona_entries_on_duty_drops_days_that_are_not_today_and_dedupes_per_rep():
    cfg = {"personas": [
        {"rep_id": "rep_sat", "days": NOT_TODAY},
        {"rep_id": "rep_dup", "days": TODAY, "instructions": BATMAN_BRIEF},
        {"rep_id": "rep_dup"},
    ]}
    assert [e["rep_id"] for e in persona_entries_on_duty(cfg, NOW)] == ["rep_dup"]


def test_personas_on_duty_still_lists_instructions_only_reps():
    """It answers "who is on duty today", which an instructions-only character still is: the flag
    excuses them from ROUTED work, it does not take them off the quest."""
    cfg = {"personas": [
        {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
         "instructions_only": True},
        {"rep_id": "rep_bailey"},
    ]}
    assert personas_on_duty(cfg, NOW) == ["rep_batman", "rep_bailey"]


def test_persona_instructions_are_found_anywhere_in_the_roster_not_just_today():
    """One character can hold several entries, and the one carrying their brief need not be the
    one that puts them on duty today. Reading the whole roster is what makes that irrelevant."""
    cfg = {"personas": [{"rep_id": "rep_batman", "days": NOT_TODAY,
                         "instructions": BATMAN_BRIEF},
                        {"rep_id": "rep_batman"}]}
    assert persona_instructions_for(cfg, "rep_batman") == BATMAN_BRIEF
    assert [e["rep_id"] for e in persona_entries_on_duty(cfg, NOW)] == ["rep_batman"]


def test_persona_instructions_are_none_when_absent_blank_or_unknown():
    cfg = {"personas": [{"rep_id": "rep_bailey"},
                        {"rep_id": "rep_blank", "instructions": "   \n "}]}
    assert persona_instructions_for(cfg, "rep_bailey") is None
    assert persona_instructions_for(cfg, "rep_blank") is None
    assert persona_instructions_for(cfg, "rep_nobody") is None
    assert persona_instructions_for(cfg, None) is None


def test_persona_instructions_are_truncated_at_the_cap_with_a_warning(caplog):
    cfg = {"personas": [{"rep_id": "rep_batman",
                         "instructions": "b" * (MAX_INSTRUCTIONS_CHARS + 50)}]}
    with caplog.at_level("WARNING"):
        resolved = persona_instructions_for(cfg, "rep_batman")
    assert resolved == "b" * MAX_INSTRUCTIONS_CHARS
    assert f"truncated from {MAX_INSTRUCTIONS_CHARS + 50} to {MAX_INSTRUCTIONS_CHARS}" in caplog.text
    assert "b" * 200 not in caplog.text          # the text itself is never logged


# --- goal routing skips instructions_only, but an explicit assignment still wins ------------------

def test_instructions_only_entry_is_invisible_to_work_routing():
    """Without this, the day-matched entry wins and the Saturday specialist absorbs everything the
    quest routes, including recurring tasks the person never named them on."""
    cfg = {"personas": [
        {"rep_id": "rep_bailey"},
        {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
         "instructions_only": True},
    ]}
    assert resolve_persona(cfg, NOW) == "rep_bailey"


def test_instructions_only_is_skipped_in_the_unrestricted_loop_too():
    cfg = {"personas": [{"rep_id": "rep_batman", "instructions": BATMAN_BRIEF,
                         "instructions_only": True}]}
    assert resolve_persona(cfg, NOW) is None
    assert resolve_persona(cfg, NOW, fallback_resolver=lambda item: "rep_cards") == "rep_cards"


def test_instructions_only_tolerates_the_string_forms_a_json_round_trip_leaves():
    for flag in ("true", "True", "1", "yes"):
        cfg = {"personas": [{"rep_id": "rep_bailey"},
                            {"rep_id": "rep_batman", "days": TODAY, "instructions_only": flag}]}
        assert resolve_persona(cfg, NOW) == "rep_bailey"
    # ...and a stray "false" must never read as enabled.
    cfg = {"personas": [{"rep_id": "rep_bailey"},
                        {"rep_id": "rep_batman", "days": TODAY, "instructions_only": "false"}]}
    assert resolve_persona(cfg, NOW) == "rep_batman"


def test_a_recurring_tasks_own_assignee_wins_even_over_instructions_only():
    """A human naming that character for that task is more specific than any roster preference."""
    cfg = {"personas": [
        {"rep_id": "rep_bailey"},
        {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
         "instructions_only": True},
    ]}
    assert resolve_task_persona({"assignee_rep_id": "rep_batman"}, cfg, NOW) == "rep_batman"


def test_an_instructions_only_rep_on_duty_still_gets_their_own_batch_with_their_own_brief():
    """``instructions_only`` keeps a character out of ROUTING, never off the quest: they are on
    duty, so they get a batch, and it carries their own brief rather than the quest's alone."""
    personas = [{"rep_id": "rep_bailey"},
                {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
                 "instructions_only": True}]
    q1 = _roster_quest(personas=personas)
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", daily_budget=5,
                           now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 2
    by_rep = {t["assignee_rep_id"]: t for t in client.created_tasks}
    assert BATMAN_BRIEF in by_rep["rep_batman"]["text"]
    assert BATMAN_BRIEF not in by_rep["rep_bailey"]["text"]


# --- one batch per character on duty (AutopilotPass) ----------------------------------------------

def test_persona_on_duty_with_instructions_and_no_goal_produces_exactly_one_batch():
    q1 = _roster_quest(personas=[{"rep_id": "rep_batman", "days": TODAY,
                                  "instructions": BATMAN_BRIEF, "instructions_only": True}])
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == "rep_batman"
    assert BATMAN_BRIEF in created["text"]
    assert "Goal: " not in created["text"]


def test_a_persona_whose_days_exclude_today_produces_nothing():
    q1 = _roster_quest(personas=[{"rep_id": "rep_batman", "days": NOT_TODAY,
                                  "instructions": BATMAN_BRIEF, "instructions_only": True}])
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert result.created_task_ids == []
    assert client.created_tasks == []


def test_a_roster_entry_with_no_instructions_of_its_own_still_gets_a_batch():
    """Being on duty IS the job now: with a default brief behind them, a character the person
    rostered for today always has something to produce."""
    q1 = _roster_quest(personas=[{"rep_id": "rep_bailey", "days": TODAY}])
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert client.created_tasks[0]["assignee_rep_id"] == "rep_bailey"


def test_two_characters_on_duty_get_two_batches_each_with_its_own_brief():
    """The case the whole feature exists for: Bailey does her job and Batman does his own standing
    review, on the same pass, each told what THEIR job is."""
    personas = [{"rep_id": "rep_bailey", "instructions": BAILEY_BRIEF},
                {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
                 "instructions_only": True}]
    q1 = _roster_quest(personas=personas, instructions="Keep every result under a page.")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})

    assert len(result.created_task_ids) == 2
    by_rep = {t["assignee_rep_id"]: t for t in client.created_tasks}
    assert BAILEY_BRIEF in by_rep["rep_bailey"]["text"]
    assert BATMAN_BRIEF not in by_rep["rep_bailey"]["text"]
    assert BATMAN_BRIEF in by_rep["rep_batman"]["text"]
    assert BAILEY_BRIEF not in by_rep["rep_batman"]["text"]
    # The quest's goal is context for BOTH of them, and work for neither.
    assert all("- Draft chapter three" in t["text"] for t in client.created_tasks)
    assert not any("Goal: Draft chapter three" in t["text"] for t in client.created_tasks)
    # Both still carry the quest-wide brief: the two layers are additive, never either/or.
    assert all("Keep every result under a page." in t["text"] for t in client.created_tasks)


def test_a_character_listed_twice_in_the_roster_still_gets_only_one_batch():
    personas = [{"rep_id": "rep_bailey", "days": TODAY, "instructions": BAILEY_BRIEF},
                {"rep_id": "rep_bailey"}]
    q1 = _roster_quest(personas=personas)
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert BAILEY_BRIEF in client.created_tasks[0]["text"]


def test_each_persona_batch_consumes_exactly_one_budget_unit():
    personas = [{"rep_id": "rep_bailey", "instructions": BAILEY_BRIEF},
                {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
                 "instructions_only": True}]
    q1 = _roster_quest(personas=personas)
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", daily_budget=2, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 2

    client = FakeAutopilotClient(quests=[_roster_quest(personas=personas)], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert any(s["quest_id"] == "q1" and "budget" in s["reason"] for s in result.skipped)


def test_a_persona_batch_never_reaches_the_goal_proposal_path():
    q1 = _roster_quest(personas=[{"rep_id": "rep_batman", "days": TODAY,
                                  "instructions": BATMAN_BRIEF, "instructions_only": True}],
                       planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert not any(c.get("kind") == "goal_proposal" for c in result.created)


# --- title fallback: goals -> adopted -> persona instructions -> quest instructions ---------------

def test_batch_title_prefers_persona_instructions_over_the_quests_own():
    title = _batch_title([], instructions="# Quest-wide brief\nmore",
                         persona_instructions="> Saturday relationship review\nmore")
    assert title == "Saturday relationship review"


def test_batch_title_falls_through_to_quest_instructions_when_the_persona_has_none():
    assert _batch_title([], instructions="Quest-wide brief",
                        persona_instructions=None) == "Quest-wide brief"


def test_batch_title_lets_an_adopted_task_name_the_batch_over_either_brief():
    title = _batch_title([{"title": "Email the morning brief"}], instructions="quest brief",
                         persona_instructions="persona brief")
    assert title == "Email the morning brief"


def test_batch_title_is_autopilot_run_when_only_blank_persona_instructions_are_given():
    assert _batch_title([], persona_instructions="  \n# \n> ") == "Autopilot run"


def test_an_instructions_only_batch_is_titled_from_the_personas_instructions():
    personas = [{"rep_id": "rep_batman", "days": TODAY, "instructions_only": True,
                 "instructions": "# Weekly relationship review\nLook at both sides."}]
    q1 = _roster_quest(personas=personas, instructions="# The quest's own daily brief\nDo it.")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert client.created_tasks[0]["title"] == "Weekly relationship review"


# --- a dry run says WHOSE instructions are driving each batch -------------------------------------

def test_dry_run_names_whose_instructions_drive_each_batch():
    personas = [{"rep_id": "rep_bailey", "instructions": BAILEY_BRIEF},
                {"rep_id": "rep_batman", "days": TODAY, "instructions": BATMAN_BRIEF,
                 "instructions_only": True}]
    q1 = _roster_quest(personas=personas, instructions="Keep it short.")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    result = AutopilotPass(client, team_id="team1", daily_budget=5,
                           now=_now).run({"text": "autopilot dry-run pass"})
    assert client.created_tasks == []
    assert len(result.proposals) == 2
    by_persona = {p["persona"]: p for p in result.proposals}
    assert "rep_bailey's own standing instructions" in by_persona["rep_bailey"]["instructions_from"]
    assert "rep_batman's own standing instructions" in by_persona["rep_batman"]["instructions_from"]
    assert "this quest's standing instructions" in by_persona["rep_batman"]["instructions_from"]
    assert "rep_batman" in result.summary_text()
