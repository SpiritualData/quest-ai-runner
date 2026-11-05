"""Rebuild cases.json for the Quest ordering metric: the thirteen cases that are Quest's own (they were removed from the hub's Prioritization because
they exercise Quest's data and rules). The expected order is written by hand from the rule, never produced by an arm."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = "2026-07-12T09:00:00Z"
BACKEND_RANK = "quest-backend order_quests_by_user_priority"
BACKEND_PROMPT = "quest-backend prompts/autopilot_prompts.py default quest instructions"


def case(name, kind, rule, source, person, candidates, expect):
    return {"id": "quest-" + name, "kind": kind, "origin": "rule: " + rule, "source": source,
            "args": {"person": person, "candidates": candidates, "now": NOW}, "expect": expect}


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
    ]
    return cases


if __name__ == "__main__":
    (HERE / "cases.json").write_text(json.dumps(build(), indent=1) + "\n")
