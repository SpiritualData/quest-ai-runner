"""The day rule: a character works a quest only on the days their roster entry names.

From a real incident. A quest had one character rostered ["Mon".."Fri"] and another rostered
["Sat"]. On a Saturday the weekday character produced work and emailed the owner, because a work
item carrying her ``assignee_rep_id`` outranked her own roster entry: the days were advisory. The
owner's instruction was that autopilot should always and only follow the user's setting of day per
character, so the days are authoritative now and nothing overrides them.

The item that still names a character is an ADOPTED RECURRING TASK, since a goal names nobody any
more (goals are context for a run, never an assignment to one). What these tests pin:

  * a recurring task assigned to a ROSTERED character who is not on duty today is HELD -- not
    adopted, left queued to run as the person scheduled it, and never re-routed to whoever happens
    to be around;
  * one assigned to a character with NO roster entry is unaffected (no entry, no day setting);
  * an unassigned recurring task routes through the day-matched-then-unrestricted roster order, and
    when the roster names work-routing characters it can never reach the consumer's fallback
    resolver or the plain assistant;
  * a day NOBODY is rostered for produces nothing at all, reported as a skip that names the day,
    decided from config and the clock alone -- before any goal is fetched;
  * a quest that never configured a roster behaves exactly as it did before any of this.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    PERSONA_HELD,
    AutopilotPass,
    resolve_persona,
    resolve_task_persona,
    split_held_for_another_day,
)

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _quest

SATURDAY = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 7, 13, 9, 0, 0, tzinfo=timezone.utc)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
BAILEY = "rep_bailey"
BATMAN = "rep_batman"
BATMAN_BRIEF = "Look at the funding work from outside and propose one decision."


def _at(when):
    """The pass's clock, pinned to one moment."""
    return lambda: when


def _rostered_quest(*, batman_instructions=None, quest_instructions=None,
                    batman_instructions_only=False):
    """The incident's own roster: the weekday worker and the Saturday character."""
    personas = [{"rep_id": BAILEY, "days": WEEKDAYS},
                {"rep_id": BATMAN, "days": ["Sat"]}]
    if batman_instructions:
        personas[1]["instructions"] = batman_instructions
    if batman_instructions_only:
        personas[1]["instructions_only"] = True
    quest = _quest("q1", personas=personas)
    if quest_instructions:
        quest["autopilot"]["instructions"] = quest_instructions
    return quest


def _baileys_recurring(rep=BAILEY):
    task = {"id": "r1", "goal_id": "q1", "status": "queued", "series_id": "s1",
            "text": "Email the morning brief"}
    if rep:
        task["assignee_rep_id"] = rep
    return [task]


def _adopting(quest):
    quest["autopilot"]["adopt_recurring"] = True
    return quest


def _run(client, when, **kwargs):
    return AutopilotPass(client, team_id="team1", daily_budget=5, now=_at(when),
                         **kwargs).run({"text": "pass"})


# --- the incident itself --------------------------------------------------------------------------

def test_a_recurring_task_assigned_to_the_weekday_character_is_not_adopted_on_saturday():
    """The exact case that shipped work on the wrong day. Her assignment no longer outranks her
    days, and the task is not passed to the Saturday character or to the plain assistant either."""
    client = FakeAutopilotClient(quests=[_adopting(_rostered_quest())], goals_by_quest={},
                                 tasks=_baileys_recurring())
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1                  # the Saturday character's own batch
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BATMAN
    assert "Email the morning brief" not in created["text"]
    # Held is never lost: the occurrence stays queued and runs as the person scheduled it.
    assert client.task_updates == []


def test_the_same_recurring_task_is_adopted_on_a_monday():
    """Held is not dropped. On a day her roster entry names, the identical occurrence is folded
    into her batch."""
    client = FakeAutopilotClient(quests=[_adopting(_rostered_quest())], goals_by_quest={},
                                 tasks=_baileys_recurring())
    result = _run(client, MONDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BAILEY
    assert "Email the morning brief" in created["text"]
    assert client.task_updates[0][0] == "r1"
    assert result.skipped == []


def test_saturday_produces_the_saturday_characters_own_batch_and_no_one_elses():
    """Holding one character's task must not make the pass silent, and must not hand that task to
    the character who IS on duty."""
    client = FakeAutopilotClient(
        quests=[_adopting(_rostered_quest(batman_instructions=BATMAN_BRIEF))],
        goals_by_quest={"q1": _goals_payload(
            ("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))},
        tasks=_baileys_recurring())
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BATMAN
    assert BATMAN_BRIEF in created["text"]
    assert "Email the morning brief" not in created["text"]
    # The quest's goal is named in the ladder as the person's plan, never as this character's work.
    assert "- Draft chapter three" in created["text"]
    assert "Goal: Draft chapter three" not in created["text"]


# --- a day nobody was rostered for ----------------------------------------------------------------

def test_a_sunday_nobody_is_rostered_for_produces_nothing_and_names_the_day():
    """Mon-Fri and Sat between them leave Sunday unrostered. This used to produce a plain-assistant
    run: the standing-instructions branch fell from the on-duty list to the fallback resolver to no
    persona at all, so a quest with instructions worked on a day its owner had rostered nobody."""
    quest = _rostered_quest(batman_instructions=BATMAN_BRIEF,
                            quest_instructions="Write today's brief.")
    client = FakeAutopilotClient(quests=[quest], goals_by_quest={})
    result = _run(client, SUNDAY)

    assert client.created_tasks == []
    assert result.created_task_ids == []
    assert [s["quest_id"] for s in result.skipped] == ["q1"]
    assert "Sunday" in result.skipped[0]["reason"]
    assert "Sunday" in result.summary_text()
    # Nothing was produced, so nothing was recorded as a pass either (the same way every other
    # gate leaves the quest's bookkeeping alone).
    assert client.autopilot_updates == []


def test_the_unrostered_day_gate_costs_nothing_beyond_config_and_the_clock():
    """It is a cheap gate and it must sit with the cheap gates: no goal fetch, and therefore no
    model call, for a quest that cannot do anything today."""
    client = FakeAutopilotClient(quests=[_rostered_quest(batman_instructions=BATMAN_BRIEF)],
                                 goals_by_quest={})
    _run(client, SUNDAY)
    assert client.goal_list_calls == []


def test_a_rostered_day_still_reaches_the_goals():
    """The other half of the gate: on a day somebody IS on duty, the pass reads the goals as
    before. Without this, "no goal fetch" would also be satisfied by never running at all."""
    client = FakeAutopilotClient(quests=[_rostered_quest()], goals_by_quest={})
    _run(client, MONDAY)
    assert client.goal_list_calls == ["q1"]


# --- who the rule applies to ----------------------------------------------------------------------

def test_a_recurring_task_assigned_to_a_character_with_no_roster_entry_runs_on_any_day():
    """No entry means the person set no days for that character, so there is no day setting to
    follow and nothing to hold the task for."""
    for when in (SATURDAY, MONDAY):
        client = FakeAutopilotClient(quests=[_adopting(_rostered_quest())], goals_by_quest={},
                                     tasks=_baileys_recurring(rep="rep_stephanie"))
        result = _run(client, when)
        adopted = [t for t in client.created_tasks
                   if "Email the morning brief" in t["text"]]
        assert len(adopted) == 1
        assert adopted[0]["assignee_rep_id"] == "rep_stephanie"
        assert result.skipped == []


def test_an_unassigned_recurring_task_is_held_rather_than_handed_to_whoever_is_on_duty():
    """The Saturday character here takes no routed work (``instructions_only``), so on Saturday the
    roster's work-routing characters are all off duty. The unassigned occurrence waits for one of
    them instead of being absorbed by the specialist."""
    quest = _adopting(_rostered_quest(batman_instructions=BATMAN_BRIEF,
                                      batman_instructions_only=True))
    client = FakeAutopilotClient(quests=[quest], goals_by_quest={},
                                 tasks=_baileys_recurring(rep=None))
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BATMAN
    assert "Email the morning brief" not in created["text"]
    assert client.task_updates == []


def test_an_unassigned_recurring_task_is_not_given_to_the_fallback_resolver_when_a_roster_exists():
    """The consumer's resolver is for quests that never configured a roster. Consulting it here
    would be a second opinion about a question the person already answered with their days."""
    quest = _adopting(_rostered_quest(batman_instructions=BATMAN_BRIEF,
                                      batman_instructions_only=True))
    client = FakeAutopilotClient(quests=[quest], goals_by_quest={},
                                 tasks=_baileys_recurring(rep=None))
    asked = []
    _run(client, SATURDAY, persona_resolver=lambda item: asked.append(item) or "rep_x")

    assert asked == []
    assert not any(t.get("assignee_rep_id") == "rep_x" for t in client.created_tasks)


# --- a quest with no roster is untouched by every one of these rules ------------------------------

def test_a_quest_with_an_empty_roster_still_consults_the_fallback_resolver():
    goals = {"q1": _goals_payload(("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[_quest("q1")], goals_by_quest=goals)
    asked = []

    def resolver(item):
        asked.append(item)
        return "rep_from_cards"

    result = _run(client, SATURDAY, persona_resolver=resolver)

    assert asked == [{}]                                      # asked about the quest, not a goal
    assert len(result.created_task_ids) == 1
    assert client.created_tasks[0]["assignee_rep_id"] == "rep_from_cards"
    assert result.skipped == []


def test_a_quest_with_an_empty_roster_still_runs_as_the_plain_assistant():
    """No roster, no resolver, and a day a roster would have excluded: the pass runs exactly as it
    did before the day rule existed."""
    goals = {"q1": _goals_payload(("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[_quest("q1")], goals_by_quest=goals)
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert "assignee_rep_id" not in created                   # the plain assistant, no character
    assert "- Draft chapter three" in created["text"]         # their goal, as context
    assert result.skipped == []
    assert client.goal_list_calls == ["q1"]                   # and no new gate in its way


def test_a_roster_of_only_instructions_only_entries_is_no_roster_for_routing():
    """That flag says "route work as though this character were not in the roster", so such a
    roster cannot hold anything: the fallback resolver and the plain assistant stay reachable."""
    cfg = {"personas": [{"rep_id": BATMAN, "days": ["Sat"], "instructions_only": True}]}
    assert resolve_persona(cfg, SATURDAY,
                           fallback_resolver=lambda item: "rep_from_cards") == "rep_from_cards"
    assert resolve_persona(cfg, SATURDAY) is None


# --- the helpers, directly ------------------------------------------------------------------------

def test_resolve_task_persona_answers_held_for_a_rostered_character_who_is_off_duty():
    cfg = {"personas": [{"rep_id": BAILEY, "days": WEEKDAYS},
                        {"rep_id": BATMAN, "days": ["Sat"]}]}
    assigned = {"id": "r1", "assignee_rep_id": BAILEY}
    assert resolve_task_persona(assigned, cfg, SATURDAY) is PERSONA_HELD
    assert resolve_task_persona(assigned, cfg, MONDAY) == BAILEY
    # And an unassigned task on a day only the Saturday character works goes to him, since he is
    # a work-routing entry here rather than an instructions-only one.
    assert resolve_task_persona({"id": "r2"}, cfg, SATURDAY) == BATMAN


def test_split_held_for_another_day_separates_without_dropping():
    cfg = {"personas": [{"rep_id": BAILEY, "days": WEEKDAYS},
                        {"rep_id": BATMAN, "days": ["Sat"]}]}
    tasks = [{"id": "r1", "assignee_rep_id": BAILEY},
             {"id": "r2", "assignee_rep_id": BATMAN},
             {"id": "r3", "assignee_rep_id": "rep_not_rostered"},
             {"id": "r4"}]
    workable, held = split_held_for_another_day(tasks, cfg, SATURDAY)
    assert [t["id"] for t in workable] == ["r2", "r3", "r4"]
    assert [t["id"] for t in held] == ["r1"]
    # Nothing is invented and nothing disappears: the two lists are the input.
    assert len(workable) + len(held) == len(tasks)


def test_split_held_for_another_day_holds_nothing_without_a_roster():
    tasks = [{"id": "r1", "assignee_rep_id": BAILEY}, {"id": "r2"}]
    workable, held = split_held_for_another_day(tasks, {}, SATURDAY)
    assert workable == tasks
    assert held == []
