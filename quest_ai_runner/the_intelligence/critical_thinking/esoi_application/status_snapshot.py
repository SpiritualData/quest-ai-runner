"""A status snapshot, composed by code before the writer, for any turn that asks where things stand (a step of ESOI Application's ensure flow).

Role: a status question ("where are we", "what is my priority", a summary to read out on a call) is answered from one dated snapshot of the quest, not from whatever the
planner happens to read or from older notes. The snapshot holds, each from one read: (1) the quest's key measure read live (``status_reading``); (2) the active priorities: the
quest's open goals that are a keyed next step or are linked to a solution of the quest's plan, ordered by the order the plan's solution records give and then by due day, each
with its due day and its ``Current value:`` line when it holds one; (3) for a question about the asker's own work, the asker's open goals on the quest, soonest due first
(overdue ones lead); (4) the quest's goals at a glance (``overview``): the counts, the overdue goals and the goals due soon, read in one call that keeps "no goals yet" apart from "not read", so a new quest gets a true answer instead of the planner's guess. A request to lay out a coming period (the analyser's ``plans_period`` report, "plan my week") is answered from the same snapshot with ``plan=True``. Whether a turn is status-shaped is the request analyser's structured report (``asks_status``), never words. Nothing here estimates a value; a part that
cannot be read is left out, and a snapshot with no part at all is None (the turn then goes the ordinary way).

A question about the asker's quests TOGETHER ("which goal should I work on first this week", the analyser's ``across_quests`` report) has no one quest to read: ``portfolio`` reads
the open goals of every quest the asker can reach, in one dated snapshot, so the reply is written from the goals themselves and never asks the person to paste them.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue, status_reading

log = logging.getLogger("quest-ai-runner.status-snapshot")
LOCATOR = "status_snapshot"
PRIORITY_MAX = 5
OWN_MAX = 8
PAGE = 100
PAGES = 10
HEAD = "STATUS SNAPSHOT READ JUST NOW (every figure carries its own date; nothing older is part of it)"
PORTFOLIO_HEAD = "STATUS SNAPSHOT READ JUST NOW OF THE ASKER'S QUESTS TOGETHER (each quest with its open goals, soonest due first; nothing older is part of it)"
PLAN_HEAD = ("PLANNING SNAPSHOT READ JUST NOW (the quest's open work for the period asked about; lay the plan out from these goals only, name no goal that is not "
             "listed, and save nothing)")
OVERVIEW_WINDOW_DAYS = 7
OVERVIEW_MAX = 8
PORTFOLIO_QUESTS = 8
PORTFOLIO_GOALS = 6


def reachable_quests(registry: Any) -> List[Dict[str, str]]:
    """The quests the asker can reach, as the placement judge takes them (``{"quest_id", "title", "state"}``), from the lane's ``find_quest`` tool. [] when the lane has none."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("find_quest") is None:
        return []
    try:
        result = registry.invoke("find_quest", {"name": ""}, ToolContext())
    except Exception as error:      # noqa: BLE001
        log.info("quests not listed: %s: %s", type(error).__name__, error)
        return []
    rows = (result.data or []) if result.ok else []
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import quest_label
    return [{"quest_id": str(r.get("quest_id") or r.get("id")), "title": quest_label(r), "state": ""} for r in rows if isinstance(r, Mapping) and (r.get("quest_id") or r.get("id"))]


def goal_rows(registry: Any, quest_id: str, **args: Any) -> List[Mapping[str, Any]]:
    """The goals the tool lists for ``args``, every page of them (up to ``PAGES`` pages of ``PAGE``): a quest with hundreds of goals keeps its priority steps past the first page."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("list_quest_goals") is None:
        return []
    rows: List[Mapping[str, Any]] = []
    for page in range(PAGES):
        try:
            result = registry.invoke("list_quest_goals", {"quest_id": quest_id, "limit": PAGE, "offset": page * PAGE, **args}, ToolContext(quest_id=quest_id))
        except Exception as error:      # noqa: BLE001
            log.info("goals not read: %s: %s", type(error).__name__, error)
            return rows
        got = [g for g in (result.data or []) if isinstance(g, Mapping)] if result.ok else []
        rows.extend(got)
        if len(got) < PAGE:
            break
    return rows


def all_goal_rows(registry: Any, quest_id: str) -> Optional[List[Mapping[str, Any]]]:
    """Every goal of the quest, done ones included, or None when the goals could not be read. Unlike ``goal_rows`` this keeps "the quest has no goals" (a read that
    succeeded with nothing in it) apart from "the goals were not read": the first is a fact the reply states, the second is never presented as one."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    if registry is None or registry.get("list_quest_goals") is None:
        return None
    rows: List[Mapping[str, Any]] = []
    for page in range(PAGES):
        try:
            result = registry.invoke("list_quest_goals", {"quest_id": quest_id, "limit": PAGE, "offset": page * PAGE, "open_only": False}, ToolContext(quest_id=quest_id))
        except Exception as error:      # noqa: BLE001
            log.info("goals not read: %s: %s", type(error).__name__, error)
            return None
        if not getattr(result, "ok", False):
            return None
        got = [g for g in (result.data or []) if isinstance(g, Mapping)]
        rows.extend(got)
        if len(got) < PAGE:
            break
    return rows


def overview(rows: List[Mapping[str, Any]], today: date, window_days: int = OVERVIEW_WINDOW_DAYS, plan: bool = False) -> str:
    """The quest's goals at a glance, from ``all_goal_rows``: the counts (done, open, overdue, due in the window), the overdue goals oldest first, the goals due in the
    window soonest first, and for a plan the open goals with no date. An empty list is said as what it is: the quest has no goals yet."""
    head = f"Goals at a glance (today is {today.isoformat()}):"
    if not rows and plan:
        return (f"{head} this quest has no goals yet. The goal list was read just now and is empty: say so plainly as a fact about the quest, never as a read "
                "that failed, and never ask to read it again.")
    if not rows:
        return (f"{head} this quest has no goals yet. The goal list was read just now and is empty: say so plainly as a fact about the quest, never as a read "
                "that failed, and never ask to read it again.")
    done = [g for g in rows if g.get("completed")]
    open_rows = [g for g in rows if not g.get("completed") and not g.get("abandoned")]
    last = today + timedelta(days=max(0, window_days))
    overdue = sorted((g for g in open_rows if g.get("due") and str(g.get("due")) < today.isoformat()), key=lambda g: str(g.get("due")))
    soon = sorted((g for g in open_rows if g.get("due") and today.isoformat() <= str(g.get("due")) <= last.isoformat()), key=lambda g: str(g.get("due")))
    undated = [g for g in open_rows if not g.get("due")]
    lines = [f"{head} {len(rows)} goals: {len(done)} done, {len(open_rows)} open, {len(overdue)} overdue, {len(soon)} due from today through {last.isoformat()}."]
    if overdue:
        lines.append("Overdue, oldest first:\n" + "\n".join(goal_line(g) for g in overdue[:OVERVIEW_MAX]) + (f"\n(and {len(overdue) - OVERVIEW_MAX} more overdue)" if len(overdue) > OVERVIEW_MAX else ""))
    if soon:
        lines.append(f"Due from today through {last.isoformat()}, soonest first:\n" + "\n".join(goal_line(g) for g in soon[:OVERVIEW_MAX])
                     + (f"\n(and {len(soon) - OVERVIEW_MAX} more)" if len(soon) > OVERVIEW_MAX else ""))
    if plan:
        # The days of the period, so a plan's day lines ("Thursday 12 November") rest on a date the turn holds: the reply check drops a clause whose date
        # is in nothing read, and before this it cut every day label out of the plan and left fragments.
        days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
        lines.append("The days of this period: " + ", ".join(f"{d.strftime('%A')} {d.isoformat()}" for d in days) + ".")
    if plan and undated:
        lines.append("Open goals with no date:\n" + "\n".join(goal_line(g) for g in undated[:OVERVIEW_MAX]) + (f"\n(and {len(undated) - OVERVIEW_MAX} more)" if len(undated) > OVERVIEW_MAX else ""))
    if not open_rows:
        lines.append("Every goal on this quest is done or set aside: there is no open goal.")
    return "\n".join(lines)


def ladder(tables: Mapping[str, Any]) -> Dict[str, int]:
    """``{solution id: rank}`` in the order the quest's solution records list them (the plan's own priority order)."""
    return {rid: i for i, rid in enumerate((tables.get("solutions") or {}))}


def priorities(registry: Any, quest_id: str, tables: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The open goals that are a keyed next step or are linked to a solution of the plan, in ladder order then soonest due; at most ``PRIORITY_MAX``."""
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_keyed_next_step
    rows = goal_rows(registry, quest_id, open_only=True) + goal_rows(registry, quest_id, open_only=True, text_contains="next step")
    seen, kept = set(), []
    for g in rows:
        key = str(g.get("id"))
        if key in seen or g.get("completed") or g.get("abandoned"):
            continue
        seen.add(key)
        if g.get("spec_solution_id") or is_keyed_next_step(g.get("title")):
            kept.append(g)
    rank = ladder(tables)
    kept.sort(key=lambda g: (rank.get(str(g.get("spec_solution_id") or ""), len(rank)), g.get("due") is None, str(g.get("due") or "")))
    return kept[:PRIORITY_MAX]


def goal_line(g: Mapping[str, Any]) -> str:
    value = milestone_cue.value_line_of(str(g.get("text") or ""))
    owner = g.get("assignee_name") or ""
    return f"- {g.get('title')} (due {g.get('due') or 'no date'}" + (f"; owner {owner}" if owner else "") + (f"; {value}" if value else "") + ")"


ABOUT_PROMPT = ("A person asked this in their quest's chat:\n\n{question}\n\nThese are milestones of the quest:\n{listing}\n\nIs the question about where one of these "
                "milestones stands? First write one short line saying what the question is about, without any id. Then, on the last line, write only that milestone's id "
                "exactly as listed, or NONE when it asks about none of them in particular.")


def milestone_asked_about(tables: Mapping[str, Any], question: str, decide: Any) -> Optional[str]:
    """The milestone a status question is about, from the model-free candidates and one cheap judgment read from its last line; None when none is."""
    ids = [rid for rid in milestone_cue.candidates(tables, question) if rid in dict(milestone_cue.solutions_of(tables))]
    if not ids or decide is None:
        return None
    solutions = dict(milestone_cue.solutions_of(tables))
    listing = "\n".join(f"{rid}: {solutions[rid].get('name')} | done when: {milestone_cue.clip(solutions[rid].get('milestones'), milestone_cue.VALUE_CHARS)}" for rid in ids)
    lines = [line for line in str(decide(ABOUT_PROMPT.format(question=str(question).strip()[:1500], listing=listing)) or "").splitlines() if line.strip()]
    settled, chosen = milestone_cue.judged_option(lines[-1] if lines else "", ids, {rid: str(solutions[rid].get("name") or "") for rid in ids})
    return chosen if settled else None


def milestone_part(registry: Any, quest_id: str, tables: Mapping[str, Any], rid: str) -> str:
    """The milestone a status question is about, as its records hold it (the metric record's dated text, which carries what blocks it, and the owners its text names),
    with its open next-step goals (due day, owner, value line)."""
    row = dict(milestone_cue.solutions_of(tables)).get(rid) or {}
    steps = [g for g in milestone_cue.linked_goals(goal_rows(registry, quest_id, open_only=True), rid) if not g.get("completed") and not g.get("abandoned")]
    text = "The milestone the question is about, as its records hold it:\n" + milestone_cue.records_read(tables, rid, row)
    return text + ("\nIts open next steps:\n" + "\n".join(goal_line(g) for g in steps) if steps else "\nIt has no open next-step goal.")


def build(registry: Any, quest_id: str, asker_user_id: str = "", own: bool = False, question: str = "", decide: Any = None,
          window_days: int = OVERVIEW_WINDOW_DAYS, plan: bool = False, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """The snapshot as one gathered read (locator ``status_snapshot``), or None when no part could be read. With ``question`` and ``decide``, a question about one
    milestone also gets that milestone's records (what blocks it, who owns it, its next step and date). ``plan`` (a request to lay out a coming period, ``window_days``
    long) adds the open goals with no date to the goals at a glance. Never raises."""
    if registry is None or not quest_id:
        return None
    try:
        parts: List[str] = []
        tables: Mapping[str, Any] = {}
        try:
            tables, _ = milestone_cue.read_through(registry, quest_id)
        except milestone_cue.LaneGap:
            tables = {}
        if tables and question:
            try:
                about = milestone_asked_about(tables, question, decide)
                if about:
                    parts.append(milestone_part(registry, quest_id, tables, about))
            except Exception as error:      # noqa: BLE001  the milestone part is an addition; the rest of the snapshot stands
                log.info("status snapshot: the milestone part was not read: %s: %s", type(error).__name__, error)
        measure = status_reading.observation(registry, quest_id)
        if measure is not None:
            parts.append(measure["text"])
        active = priorities(registry, quest_id, tables)
        if active:
            parts.append("Active priorities, most important first:\n" + "\n".join(goal_line(g) for g in active))
        everything = all_goal_rows(registry, quest_id)
        if everything is not None:
            parts.append(overview(list(everything), today or date.today(), window_days, plan=plan))
        if own and asker_user_id:
            mine = [g for g in goal_rows(registry, quest_id, open_only=True, assignee_user_id=asker_user_id) if not g.get("completed") and not g.get("abandoned")][:OWN_MAX]
            if mine:
                parts.append("The asker's own open goals on this quest, soonest due first (overdue ones lead):\n" + "\n".join(goal_line(g) for g in mine))
        if not parts:
            return None
        return {"kind": "query", "locator": LOCATOR, "text": (PLAN_HEAD if plan else HEAD) + ":\n\n" + "\n\n".join(parts)}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("status snapshot not built: %s: %s", type(error).__name__, error)
        return None


def portfolio(registry: Any, quests: List[Mapping[str, str]]) -> Optional[Dict[str, Any]]:
    """The asker's quests together as one gathered read (locator ``status_snapshot``): per quest (at most ``PORTFOLIO_QUESTS``) its outcome and its open goals, soonest due
    first (at most ``PORTFOLIO_GOALS``), each with its due day, owner and value line. ``quests`` is ``reachable_quests``. None when no quest has an open goal to show.
    Never raises."""
    if registry is None or not quests:
        return None
    try:
        blocks: List[str] = []
        for quest in quests[:PORTFOLIO_QUESTS]:
            quest_id = str(quest.get("quest_id") or "")
            if not quest_id:
                continue
            rows = [g for g in goal_rows(registry, quest_id, open_only=True) if not g.get("completed") and not g.get("abandoned")]
            rows.sort(key=lambda g: (g.get("due") is None, str(g.get("due") or "")))
            title = str(quest.get("title") or "").strip() or "Untitled quest"
            if rows:
                blocks.append(f"Quest: {title}\n" + "\n".join(goal_line(g) for g in rows[:PORTFOLIO_GOALS]) + (f"\n(and {len(rows) - PORTFOLIO_GOALS} more open goals)" if len(rows) > PORTFOLIO_GOALS else ""))
            else:
                blocks.append(f"Quest: {title}\nNo open goals.")
        if not any("No open goals." not in b for b in blocks):
            return None
        return {"kind": "query", "locator": LOCATOR, "text": PORTFOLIO_HEAD + ":\n\n" + "\n\n".join(blocks)}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("portfolio snapshot not built: %s: %s", type(error).__name__, error)
        return None
