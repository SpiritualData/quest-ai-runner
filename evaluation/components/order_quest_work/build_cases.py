"""Rebuild cases.json for the Quest ordering metric: the cases that are Quest's own (they were removed from the hub's Prioritization because
they exercise Quest's data and rules). The expected order is written by hand from the rule, never produced by an arm."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = "2026-07-12T09:00:00Z"
BACKEND_RANK = "quest-backend order_quests_by_user_priority"
BACKEND_PROMPT = "quest-backend prompts/autopilot_prompts.py default quest instructions"


USES = {"quest-list": "goals", "goal-ladder": "goals", "goals": "goals", "work-order": "work", "open-asks": "asks", "asks-for-you": "asks"}


# The level of a goal a case orders (use goals): the person's quests, a quest's solution milestones, its period goals, the day's goals (tasks). Work and asks carry none.
LEVELS = {"quest-list": "quest", "goal-ladder": "period", "goals": "period", "work-order": "", "open-asks": "", "asks-for-you": "", "milestones": "milestone", "daily": "daily"}
USES.update({"milestones": "goals", "daily": "goals"})


def case(name, kind, rule, source, person, candidates, expect, level="", stated=None):
    built = {"id": "quest-" + name, "kind": kind, "use": USES[kind], "level": level or LEVELS[kind], "origin": "rule: " + rule, "source": source,
            "args": {"person": person, "candidates": candidates, "now": NOW}, "expect": expect}
    if stated:
        built["args"]["stated"] = stated
    return built


def quest(ident, owner="", rank=None, by_person=None):
    item = {"id": ident, "kind": "quest", "owner": owner}
    if rank is not None:
        item["rank"] = rank
    if by_person:
        item["rank_by_person"] = by_person
    return item


def goal(ident, horizon="", deadline=""):
    item = {"id": ident, "kind": "goal"}
    if horizon:
        item["horizon"] = horizon
    if deadline:
        item["deadline"] = deadline
    return item


def milestone(ident, nudged="", rank=None):
    item = {"id": ident, "kind": "solution-with-milestone"}
    if nudged:
        item["last_nudged_at"] = nudged
    if rank is not None:
        item["rank"] = rank
    return item


def decision(ident, created, deadline=""):
    item = {"id": ident, "kind": "decision", "created_at": created}
    if deadline:
        item["deadline"] = deadline
    return item


def build():
    cases = [
        case("cap-keeps-highest-priority", "quest-list", "ranked quests keep their place, so a cap of fifty keeps the highest priority", "quest-backend tests/test_quest_ranking_all.py",
             "u", [quest(f"q{i}", "u", rank=i) for i in range(50)], [f"q{i}" for i in range(50)]),
        case("member-ranks-team-quest-above-own", "quest-list", "a person's own entry in priority_by_user decides, whoever created the quest", "quest-backend tests/business/test_accessible_quests.py",
             "member", [quest("member-quest", "member", by_person={"member": 1}), quest("team-quest", "creator", by_person={"member": 0})], ["team-quest", "member-quest"]),
        case("unranked-after-ranked", "quest-list", "ranked ascending first (0 is highest), unranked after in the order given", BACKEND_RANK,
             "u", [quest("u1", "u"), quest("r2", "u", rank=2), quest("r0", "u", rank=0), quest("u2", "u")], ["r0", "r2", "u1", "u2"]),
        case("legacy-rank-is-the-creators-only", "quest-list", "the legacy priority of the quest model counts only for the quest's creator", BACKEND_RANK + " (field priority)",
             "p", [quest("other-creators", "o", rank=0), quest("mine", "p", rank=3)], ["mine", "other-creators"]),
        case("own-entry-beats-legacy-rank", "quest-list", "a person's own priority_by_user entry wins over the legacy priority", BACKEND_RANK + " (field priority_by_user)",
             "p", [quest("y", "o", rank=0, by_person={"p": 5}), quest("x", "p", rank=3)], ["x", "y"]),
        case("work-asks-then-horizons-then-ideas", "work-order", "open asks first, then goals of the shorter horizon, then the system's own ideas", BACKEND_PROMPT,
             "", [{"id": "i1", "kind": "idea"}, goal("g-year", "year"), goal("g-today", "today"), {"id": "a1", "kind": "ask"}, goal("g-week", "week")],
             ["a1", "g-today", "g-week", "g-year", "i1"]),
        case("work-asks-keep-the-order-given", "work-order", "asks keep the order they were given in", BACKEND_PROMPT,
             "", [goal("g-today", "today"), {"id": "a1", "kind": "ask"}, {"id": "a2", "kind": "ask"}], ["a1", "a2", "g-today"]),
        case("work-month-before-quarter", "work-order", "a month goal comes before a quarter goal", BACKEND_PROMPT,
             "", [goal("g-quarter", "quarter"), goal("g-month", "month")], ["g-month", "g-quarter"]),
        case("work-a-goal-before-an-idea", "work-order", "a goal comes before an idea", BACKEND_PROMPT,
             "", [{"id": "i1", "kind": "idea"}, goal("g-quarter", "quarter")], ["g-quarter", "i1"]),
        case("work-horizon-before-deadline", "work-order", "the shorter horizon comes before the earlier deadline", "quest-ai-runner goal ladder (autopilot_periods)",
             "", [goal("g-week-late", "week", "2026-07-18"), goal("g-today", "today", "2026-07-13")], ["g-today", "g-week-late"]),
        case("ladder-nearest-deadline-first", "goal-ladder", "inside a horizon the nearest deadline comes first", "quest-ai-runner tests/test_autopilot_goal_ladder.py",
             "", [goal(f"m{d}", "month", f"2026-07-{d:02d}") for d in range(20, 8, -1)], [f"m{d}" for d in range(9, 21)]),
        case("ladder-undated-last-in-plan-order", "goal-ladder", "inside a horizon undated goals come last, in the order of the plan", "quest-ai-runner tests/test_autopilot_goal_ladder.py",
             "", [goal("m1", "month"), goal("m2", "month", "2026-07-30"), goal("m3", "month"), goal("m4", "month", "2026-07-14")], ["m4", "m2", "m1", "m3"]),
        case("asks-oldest-first", "open-asks", "open asks come oldest first", "quest-ai-runner tests/test_quest_asks_brief.py",
             "", [{"id": "k_new", "kind": "ask", "asked_at": "2026-10-15T09:00:00Z"}, {"id": "k_open", "kind": "ask", "asked_at": "2026-10-12T12:00:00Z"}], ["k_open", "k_new"]),
        case("asks-for-you-overdue-then-nearest-deadline", "asks-for-you", "asks put to the person: the overdue first, then the nearer deadline, an ask with no deadline after every one that has one",
             "quest-backend business/teams/asks_digest.py sort_for_digest", "",
             [decision("d-none", "2026-07-10T08:00:00Z"), decision("d-soon", "2026-07-10T08:00:00Z", "2026-07-13T09:00:00Z"), decision("d-overdue", "2026-07-09T08:00:00Z", "2026-07-11T09:00:00Z")],
             ["d-overdue", "d-soon", "d-none"]),
        case("asks-for-you-undated-newest-first", "asks-for-you", "asks put to the person with no deadline: the newest first", "quest-backend business/teams/team_operations.py list_open_decisions_for_user",
             "", [decision("d-old", "2026-07-01T08:00:00Z"), decision("d-new", "2026-07-11T08:00:00Z"), decision("d-mid", "2026-07-05T08:00:00Z")], ["d-new", "d-mid", "d-old"]),
        case("goals-completed-last", "goals", "the person's goals: open goals by nearest deadline, undated after, a completed goal after every open one", "quest-backend business/quests/my_responsibilities.py goal_sort_key",
             "", [{"id": "g-done", "kind": "goal", "completed": True, "deadline": "2026-07-13"}, goal("g-none"), goal("g-late", deadline="2026-07-30"), goal("g-soon", deadline="2026-07-14")],
             ["g-soon", "g-late", "g-none", "g-done"]),
        case("milestones-gap-then-unmeasured-then-oldest", "milestones", "solutions whose milestone is next: the one with no metric first, then one never measured, then the metric measured longest ago",
             "quest-ai-runner tests/test_autopilot_esoi.py test_gap_first_then_unmeasured_then_oldest", "",
             [milestone("s1", nudged="2026-10-26T00:00:00+00:00"), milestone("s2", nudged="2026-10-20T00:00:00+00:00"), milestone("s3", rank=0), milestone("s4", rank=1)],
             ["s3", "s4", "s2", "s1"]),
        case("milestones-with-a-milestone-first", "milestones", "a solution with a milestone comes before one without, whatever its metric says", "quest-ai-runner autopilot_esoi.pick_solution (a milestone is the next priority)",
             "", [{"id": "bare", "kind": "solution", "rank": 0}, milestone("planned", nudged="2026-10-26T00:00:00+00:00")], ["planned", "bare"], stated={"kind_order": ["solution-with-milestone", "solution"]}),
        case("period-goal-without-a-metric-orders-as-its-milestone", "goals", "a period goal is the milestone of the solution it is for, one to one, so it orders by horizon and deadline whether or not it has a metric value",
             "Joshua 2026-11-05 (one to one with solution milestones, a goal may have no metric value); quest-backend models/planning.py spec_solution_id, spec_metric_id and metric_target_value are optional",
             "", [{"id": "g-metric", "kind": "goal", "horizon": "month", "deadline": "2026-07-20", "solution_id": "s1", "metric_target_value": 10},
                  {"id": "g-no-metric", "kind": "goal", "horizon": "month", "deadline": "2026-07-15", "solution_id": "s1"},
                  {"id": "g-no-solution", "kind": "goal", "horizon": "month", "deadline": "2026-07-18"}], ["g-no-metric", "g-no-solution", "g-metric"]),
        case("daily-ai-handled-last", "daily", "the day's goals: the person's own before a goal their AI rep handles, otherwise in the order given", "quest-backend api/endpoints/planning.py sort_ai_handled_last",
             "", [{"id": "g-rep", "kind": "goal", "ai_handling": "rep"}, goal("g-mine-1"), goal("g-mine-2")], ["g-mine-1", "g-mine-2", "g-rep"]),
    ]
    return cases


if __name__ == "__main__":
    (HERE / "cases.json").write_text(json.dumps(build(), indent=1) + "\n")
