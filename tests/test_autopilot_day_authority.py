"""The day rule: a character works a quest only on the days their roster entry names.

From a real incident. A quest had one character rostered ["Mon".."Fri"] and another rostered
["Sat"]. On a Saturday the weekday character produced work and emailed the owner, because a goal
carrying her ``assignee_rep_id`` outranked her own roster entry: the days were advisory. The
owner's instruction was that autopilot should always and only follow the user's setting of day per
character, so the days are authoritative now and nothing overrides them.

What that means, and what these tests pin:

  * a goal assigned to a ROSTERED character who is not on duty today is HELD -- excluded from
    today's targets and picked up on a day that character works this quest. Never re-routed: a
    goal handed to whoever happens to be around is the same mistake as running it on the wrong day;
  * a goal assigned to a character with NO roster entry is unaffected (no entry, no day setting);
  * an unassigned goal routes through the day-matched-then-unrestricted roster order, and when the
    roster names goal-working characters it can never reach the consumer's fallback resolver or the
    plain assistant;
  * a day NOBODY is rostered for produces nothing at all, reported as a skip that names the day,
    decided from config and the clock alone -- before any goal is fetched;
  * a quest that never configured a roster behaves exactly as it did before any of this.

This supersedes the earlier design note that a persona with a goal assigned to it is activated
whenever that goal comes due, independent of the quest's day schedule. The day schedule wins.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    PERSONA_HELD,
    AutopilotPass,
    resolve_persona,
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


def _baileys_goal(period):
    return {"q1": _goals_payload(("day", period, [
        _goal("g1", "Draft chapter three", assignee_rep_id=BAILEY)]))}


def _run(client, when, **kwargs):
    return AutopilotPass(client, team_id="team1", daily_budget=5, now=_at(when),
                         **kwargs).run({"text": "pass"})


# --- the incident itself --------------------------------------------------------------------------

def test_a_goal_assigned_to_the_weekday_character_is_held_on_saturday():
    """The exact case that shipped work on the wrong day. Her assignment no longer outranks her
    days, and the goal is not passed to the Saturday character or to the plain assistant either."""
    client = FakeAutopilotClient(quests=[_rostered_quest()],
                                 goals_by_quest=_baileys_goal("2026-07-11"))
    result = _run(client, SATURDAY)

    assert result.created_task_ids == []
    assert client.created_tasks == []                       # nothing for her, and nothing for
    assert not any(t.get("assignee_rep_id") == BATMAN       # anyone else standing around
                   for t in client.created_tasks)
    assert not any("Draft chapter three" in t["text"] for t in client.created_tasks)
    # The quest is not silently quiet: it says the work is waiting for the day its character works.
    assert [s["quest_id"] for s in result.skipped] == ["q1"]
    assert "Saturday" in result.skipped[0]["reason"]


def test_several_held_goals_are_reported_as_a_finished_sentence():
    """The report a person reads, not a template: never "{n} pieces" and never "piece(s)"."""
    goals = {"q1": _goals_payload(("day", "2026-07-11", [
        _goal("g1", "Draft chapter three", assignee_rep_id=BAILEY),
        _goal("g2", "Draft chapter four", assignee_rep_id=BAILEY)]))}
    client = FakeAutopilotClient(quests=[_rostered_quest()], goals_by_quest=goals)
    result = _run(client, SATURDAY)
    reason = result.skipped[0]["reason"]

    assert reason.startswith("2 pieces of work due belong to characters who are not rostered for "
                             "Saturday")
    assert "{n}" not in reason and "(s)" not in reason
    assert reason in result.summary_text()


def test_the_same_goal_is_worked_on_a_monday():
    """Held is not dropped. On a day her roster entry names, the identical goal runs as her."""
    client = FakeAutopilotClient(quests=[_rostered_quest()],
                                 goals_by_quest=_baileys_goal("2026-07-13"))
    result = _run(client, MONDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BAILEY
    assert "Goal: Draft chapter three" in created["text"]
    assert result.skipped == []


def test_saturday_still_produces_the_saturday_characters_own_batch():
    """Holding one character's goal must not make the pass silent. The character the person DID
    roster for Saturday still does their own standing job, and does not inherit the held goal."""
    client = FakeAutopilotClient(quests=[_rostered_quest(batman_instructions=BATMAN_BRIEF)],
                                 goals_by_quest=_baileys_goal("2026-07-11"))
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BATMAN
    assert BATMAN_BRIEF in created["text"]
    assert "Goal: " not in created["text"]
    assert "Draft chapter three" not in created["text"]


# --- a day nobody was rostered for ----------------------------------------------------------------

def test_a_sunday_nobody_is_rostered_for_produces_nothing_and_names_the_day():
    """Mon-Fri and Sat between them leave Sunday unrostered. This used to produce a plain-assistant
    run: the standing-instructions branch fell from the on-duty list to the fallback resolver to no
    persona at all, so a quest with instructions worked on a day its owner had rostered nobody."""
    quest = _rostered_quest(batman_instructions=BATMAN_BRIEF,
                            quest_instructions="Write today's brief.")
    client = FakeAutopilotClient(quests=[quest], goals_by_quest=_baileys_goal("2026-07-12"))
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
    description fetch and no model call, for a quest that cannot do anything today."""
    client = FakeAutopilotClient(quests=[_rostered_quest(batman_instructions=BATMAN_BRIEF)],
                                 goals_by_quest=_baileys_goal("2026-07-12"))
    _run(client, SUNDAY)
    assert client.goal_list_calls == []


def test_a_rostered_day_still_reaches_the_goals():
    """The other half of the gate: on a day somebody IS on duty, the pass reads the goals as
    before. Without this, "no goal fetch" would also be satisfied by never running at all."""
    client = FakeAutopilotClient(quests=[_rostered_quest()],
                                 goals_by_quest=_baileys_goal("2026-07-13"))
    _run(client, MONDAY)
    assert client.goal_list_calls == ["q1"]


# --- who the rule applies to ----------------------------------------------------------------------

def test_a_goal_assigned_to_a_character_with_no_roster_entry_runs_on_any_day():
    """No entry means the person set no days for that character, so there is no day setting to
    follow and nothing to hold the goal for."""
    for when, period in ((SATURDAY, "2026-07-11"), (MONDAY, "2026-07-13")):
        goals = {"q1": _goals_payload(("day", period, [
            _goal("g1", "Call the registrar", assignee_rep_id="rep_stephanie")]))}
        client = FakeAutopilotClient(quests=[_rostered_quest()], goals_by_quest=goals)
        result = _run(client, when)
        assert len(result.created_task_ids) == 1
        assert client.created_tasks[0]["assignee_rep_id"] == "rep_stephanie"


def test_an_unassigned_goal_is_held_rather_than_handed_to_whoever_is_on_duty():
    """The Saturday character here takes no goals (``instructions_only``), so on Saturday the
    roster's goal-working characters are all off duty. The unassigned goal waits for one of them
    instead of being absorbed by the specialist or run by the plain assistant."""
    quest = _rostered_quest(batman_instructions=BATMAN_BRIEF, batman_instructions_only=True)
    goals = {"q1": _goals_payload(("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[quest], goals_by_quest=goals)
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == BATMAN
    assert "Goal: " not in created["text"]
    assert "Draft chapter three" not in created["text"]


def test_an_unassigned_goal_is_not_given_to_the_fallback_resolver_when_a_roster_exists():
    """The consumer's resolver is for quests that never configured a roster. Consulting it here
    would be a second opinion about a question the person already answered with their days."""
    quest = _rostered_quest(batman_instructions=BATMAN_BRIEF, batman_instructions_only=True)
    goals = {"q1": _goals_payload(("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[quest], goals_by_quest=goals)
    asked = []
    _run(client, SATURDAY, persona_resolver=lambda goal: asked.append(goal.get("id")) or "rep_x")

    assert asked == []
    assert not any(t.get("assignee_rep_id") == "rep_x" for t in client.created_tasks)


def test_a_recurring_task_assigned_to_an_off_duty_character_is_not_adopted():
    """An adopted task becomes a batch for whoever it names, so it is under the same rule. Held
    means simply not adopted: the occurrence stays queued and runs as the person scheduled it."""
    quest = _rostered_quest(batman_instructions=BATMAN_BRIEF)
    quest["autopilot"]["adopt_recurring"] = True
    tasks = [{"id": "r1", "goal_id": "q1", "status": "queued", "series_id": "s1",
              "text": "Email the morning brief", "assignee_rep_id": BAILEY}]
    client = FakeAutopilotClient(quests=[quest], goals_by_quest={}, tasks=tasks)
    result = _run(client, SATURDAY)

    assert len(result.created_task_ids) == 1                  # the Saturday character's own batch
    assert client.created_tasks[0]["assignee_rep_id"] == BATMAN
    assert "Email the morning brief" not in client.created_tasks[0]["text"]
    assert client.task_updates == []                          # never closed, so never lost


# --- a quest with no roster is untouched by every one of these rules ------------------------------

def test_a_quest_with_an_empty_roster_still_consults_the_fallback_resolver():
    goals = {"q1": _goals_payload(("day", "2026-07-11", [_goal("g1", "Draft chapter three")]))}
    client = FakeAutopilotClient(quests=[_quest("q1")], goals_by_quest=goals)
    asked = []

    def resolver(goal):
        asked.append(goal.get("id"))
        return "rep_from_cards"

    result = _run(client, SATURDAY, persona_resolver=resolver)

    assert asked == ["g1"]
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
    assert "Goal: Draft chapter three" in created["text"]
    assert result.skipped == []
    assert client.goal_list_calls == ["q1"]                   # and no new gate in its way


def test_a_roster_of_only_instructions_only_entries_is_no_roster_for_goal_routing():
    """That flag says "route goals as though this character were not in the roster", so such a
    roster cannot hold a goal: the fallback resolver and the plain assistant stay reachable."""
    cfg = {"personas": [{"rep_id": BATMAN, "days": ["Sat"], "instructions_only": True}]}
    assert resolve_persona(_goal("g1"), cfg, SATURDAY,
                           fallback_resolver=lambda g: "rep_from_cards") == "rep_from_cards"
    assert resolve_persona(_goal("g1"), cfg, SATURDAY) is None


# --- the helpers, directly ------------------------------------------------------------------------

def test_resolve_persona_answers_held_for_a_rostered_character_who_is_off_duty():
    cfg = {"personas": [{"rep_id": BAILEY, "days": WEEKDAYS},
                        {"rep_id": BATMAN, "days": ["Sat"]}]}
    assigned = _goal("g1", assignee_rep_id=BAILEY)
    assert resolve_persona(assigned, cfg, SATURDAY) is PERSONA_HELD
    assert resolve_persona(assigned, cfg, MONDAY) == BAILEY
    # And an unassigned goal on a day only the Saturday character works goes to him, since he is
    # a goal-working entry here rather than an instructions-only one.
    assert resolve_persona(_goal("g2"), cfg, SATURDAY) == BATMAN


def test_split_held_for_another_day_separates_without_dropping():
    cfg = {"personas": [{"rep_id": BAILEY, "days": WEEKDAYS},
                        {"rep_id": BATMAN, "days": ["Sat"]}]}
    items = [_goal("g1", assignee_rep_id=BAILEY),
             _goal("g2", assignee_rep_id=BATMAN),
             _goal("g3", assignee_rep_id="rep_not_rostered"),
             _goal("g4")]
    workable, held = split_held_for_another_day(items, cfg, SATURDAY)
    assert [g["id"] for g in workable] == ["g2", "g3", "g4"]
    assert [g["id"] for g in held] == ["g1"]
    # Nothing is invented and nothing disappears: the two lists are the input.
    assert len(workable) + len(held) == len(items)


def test_split_held_for_another_day_holds_nothing_without_a_roster():
    items = [_goal("g1", assignee_rep_id=BAILEY), _goal("g2")]
    workable, held = split_held_for_another_day(items, {}, SATURDAY)
    assert workable == items
    assert held == []
