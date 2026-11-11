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
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue, status_reading

log = logging.getLogger("quest-ai-runner.status-snapshot")
# A turn's one-sentence call reports on the orchestrator's log, the one the server shows (this module's own is not routed there)
turn_log = logging.getLogger("quest-ai-runner.orchestrator")
LOCATOR = "status_snapshot"
PRIORITY_MAX = 5
OWN_MAX = 8
PAGE = 100
PAGES = 10
HEAD = "STATUS SNAPSHOT READ JUST NOW (every figure carries its own date; nothing older is part of it)"
PORTFOLIO_HEAD = "STATUS SNAPSHOT READ JUST NOW OF THE ASKER'S QUESTS TOGETHER (each quest with its open goals, soonest due first; nothing older is part of it)"
PORTFOLIO_PLAN_HEAD = ("PLANNING SNAPSHOT READ JUST NOW OF THE ASKER'S QUESTS TOGETHER (no quest is open in this chat: each quest with its open goals, soonest "
                       "due first; lay the plan out from these goals only, naming each goal's quest, name no goal that is not listed, and save nothing)")
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
    return [{"quest_id": str(r.get("quest_id") or r.get("id")), "title": quest_label(r), "name": str(r.get("name") or "").strip(),
             "outcome": " ".join(str(r.get("outcome") or "").split()), "state": "", "started": str(r.get("created_at") or r.get("start_date") or "")}
            for r in rows if isinstance(r, Mapping) and (r.get("quest_id") or r.get("id"))]


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
        # Measured 2026-11-11: told only that the list is empty, the writer offered to propose goals ("Should I propose them?") instead of writing them; and with
        # no day of the period in the snapshot, the dates it gave its proposals were cut as stated by nothing, leaving broken list lines.
        days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
        return (f"{head} this quest has no goals yet. The goal list was read just now and is empty: say so plainly as a fact about the quest, never as a read "
                "that failed, and never ask to read it again. Then write out, in this reply, up to three proposed first goals that move the quest's outcome, "
                "as day lines of this period like \"- Thursday 12 November: Choose a first piece to learn\", the first for today or tomorrow: never only offer "
                "to propose them. When the reply's first sentence is written by code, it already says these are proposals: no line says it again.\n"
                "The days of this period: " + ", ".join(said_day(d) for d in days) + ".")
    if not rows:
        return (f"{head} this quest has no goals yet. The goal list was read just now and is empty: say so plainly as a fact about the quest, never as a read "
                "that failed, and never ask to read it again.")
    done = [g for g in rows if g.get("completed")]
    open_rows = [g for g in rows if not g.get("completed") and not g.get("abandoned")]
    last = today + timedelta(days=max(0, window_days))
    overdue = sorted((g for g in open_rows if g.get("due") and str(g.get("due")) < today.isoformat()), key=lambda g: str(g.get("due")))
    soon = sorted((g for g in open_rows if g.get("due") and today.isoformat() <= str(g.get("due")) <= last.isoformat()), key=lambda g: str(g.get("due")))
    undated = [g for g in open_rows if not g.get("due")]
    lines = [f"{head} {len(rows)} goals: {len(done)} done, {len(open_rows)} open, {len(overdue)} overdue, {len(soon)} due by {last.day} {last.strftime('%B')}."]       # a person's date: an ISO date here was copied into replies (2026-11-11)
    if done and not plan:
        # Measured 2026-11-11 ("How is this quest going?" on a quest with one goal done): the status never named what was already achieved, so it read as
        # only a list of what is late. The done goals are named so progress can be said, not just the backlog.
        lines.append("Done so far (up to three of the completed goals):\n" + "\n".join(f"- {g.get('title')}" for g in done[-3:]))
    if overdue:
        lines.append("Overdue, oldest first:\n" + "\n".join(goal_line(g, today) for g in overdue[:OVERVIEW_MAX]) + (f"\n(and {len(overdue) - OVERVIEW_MAX} more overdue)" if len(overdue) > OVERVIEW_MAX else ""))
    if soon:
        lines.append(f"Due from today through {last.isoformat()}, soonest first:\n" + "\n".join(goal_line(g) for g in soon[:OVERVIEW_MAX])
                     + (f"\n(and {len(soon) - OVERVIEW_MAX} more)" if len(soon) > OVERVIEW_MAX else ""))
    later = sorted((g for g in open_rows if g.get("due") and str(g.get("due"))[:10] > last.isoformat()), key=lambda g: str(g.get("due")))
    if not plan and (later or undated):
        # Every open goal is in a status read, not only the week's: measured live 2026-11-11, "Set up the shop page" (due 30 November) was "not listed
        # among this quest's goals in the status read", and the code-written 'After that, "Choose the illustrator" is due 31 December.' was cut as
        # stating a date nothing read held.
        lines.append(f"Due after {last.day} {last.strftime('%B')}, soonest first, then those with no date:\n"
                     + "\n".join(goal_line(g) for g in (later + undated)[:OVERVIEW_MAX])
                     + (f"\n(and {len(later) + len(undated) - OVERVIEW_MAX} more)" if len(later) + len(undated) > OVERVIEW_MAX else ""))
    if plan:
        # The days of the period, so a plan's day lines ("Thursday 12 November") rest on a date the turn holds: the reply check drops a clause whose date
        # is in nothing read, and before this it cut every day label out of the plan and left fragments.
        days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
        lines.append("The days of this period: " + ", ".join(said_day(d) for d in days) + ".")
        layout = day_layout(open_rows, today, window_days)
        if layout:
            lines.append("Day layout worked out by code (every overdue goal and every goal due in the period has a day, on or before its due day, at most "
                         f"{PLAN_PER_DAY} a day while there is room; write the plan from this layout, keeping each goal on its day):\n" + layout)
    if plan and undated:
        lines.append("Open goals with no date:\n" + "\n".join(goal_line(g) for g in undated[:OVERVIEW_MAX]) + (f"\n(and {len(undated) - OVERVIEW_MAX} more)" if len(undated) > OVERVIEW_MAX else ""))
    if not open_rows:
        lines.append("Every goal on this quest is done or set aside: there is no open goal.")
    return "\n".join(lines)


PLAN_PER_DAY = 2


def said_date(value: Any) -> str:
    """A due day as a person reads it ("8 November"), from an ISO date; the value as it is when it is not one. The snapshot says dates this way
    because the reply check holds every date of a reply to what the turn read: listed only as 2026-11-12, "Thursday 12 November" in a week plan read
    as in nothing read, and the repair round that followed rewrote the plan and named a goal twice in one line (measured live 2026-11-11)."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.day} {d.strftime('%B')}"


def said_day(d: date) -> str:
    """A day of a period as a person reads it, with its ISO date for the record: "Thursday 12 November (2026-11-12)"."""
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')} ({d.isoformat()})"


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def named_day(g: Mapping[str, Any], days: List[date], limit: date) -> Optional[date]:
    """The day of the period a goal's own name sets ("Long run of 12 km on Saturday" is Saturday's), when that day falls in the period on or before
    ``limit``; else None. Measured 2026-11-11: the earliest-day rule put a Saturday long run on Thursday."""
    words = set(re.findall(r"[a-z]+", str(g.get("title") or "").lower()))
    said = [w for w in WEEKDAYS if w in words]
    if len(said) != 1:
        return None
    return next((d for d in days if d <= limit and d.strftime("%A").lower() == said[0]), None)


def day_layout(open_rows: List[Mapping[str, Any]], today: date, window_days: int = OVERVIEW_WINDOW_DAYS) -> str:
    """The plan's days as code places them: the overdue goals first (oldest first), then the goals due in the period (soonest first), each on the earliest
    day with room (``PLAN_PER_DAY``) that is not after its due day, and when there is no such day, on its due day anyway; then the open goals with no date
    fill the days left empty. Measured 2026-11-11: left to the writer, week plans on busy quests dropped overdue goals ("Run 10 customer interviews") and
    put two days' work under one label. "" when there is nothing to place."""
    days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
    last = days[-1]
    overdue = sorted((g for g in open_rows if g.get("due") and str(g.get("due"))[:10] < today.isoformat()), key=lambda g: str(g.get("due")))
    due = sorted((g for g in open_rows if g.get("due") and today.isoformat() <= str(g.get("due"))[:10] <= last.isoformat()), key=lambda g: str(g.get("due")))
    undated = [g for g in open_rows if not g.get("due")]
    placed: Dict[date, List[str]] = {d: [] for d in days}
    unplaced: List[Mapping[str, Any]] = []
    for g in overdue + due:
        limit = date.fromisoformat(str(g.get("due"))[:10]) if str(g.get("due"))[:10] >= today.isoformat() else last
        named = named_day(g, days, limit)
        room = named or next((d for d in days if d <= limit and len(placed[d]) < PLAN_PER_DAY), None)
        if room is None and g in due:
            room = limit
        if room is None:
            unplaced.append(g)
            continue
        late = days_overdue(g, today)
        placed[room].append(f"{g.get('title')} (" + (f"overdue, due {said_date(g.get('due'))}" if late else f"due {said_date(g.get('due'))}")
                            + (f"; carried over from {g.get('carried_from')}" if g.get("carried_from") else "") + ")")
    for g in undated:
        empty = next((d for d in days if not placed[d]), None)
        if empty is None:
            break
        placed[empty].append(f"{g.get('title')} (no date)")
    if not any(placed.values()):
        return ""
    out = [f"- {d.strftime('%A')} {d.day} {d.strftime('%B')}: " + "; ".join(placed[d]) for d in days if placed[d]]
    if unplaced:
        out.append(f"(and {len(unplaced)} more overdue goals with no room this period: " + "; ".join(str(g.get("title")) for g in unplaced[:OVERVIEW_MAX]) + ")")
    return "\n".join(out)


def lead_sentence(rows: List[Mapping[str, Any]], today: date, quest_name: str = "", aim: str = "", next_step: bool = False, started: str = "") -> str:
    """The status reply's first sentence, written by code from the goal counts ("As of 11 November, none of the quest's 25 goals is done yet, and 7 are
    overdue."), so the overall picture is always stated and stated right. "" for a quest with no goals (the reply says that itself). ``quest_name`` names
    the quest in it ("1 of the 4 goals in \"Run my first half marathon\" is done"): measured 2026-11-11, a reply that opened "the quest's 2 goals" never
    named the quest at all on two of four test accounts, and one called its outcome "The quest 'I sell 20 pieces online'"."""
    if not rows:
        return no_goals_lead(quest_name, outcome=aim_outcome(aim), next_step=next_step, started=started, today=today)
    if next_step:
        return next_step_lead(rows, today, quest_name, aim_outcome(aim))
    done = sum(1 for g in rows if g.get("completed"))
    open_rows = [g for g in rows if not g.get("completed") and not g.get("abandoned")]
    late = sum(1 for g in open_rows if g.get("due") and str(g.get("due"))[:10] < today.isoformat())
    when = f"{today.day} {today.strftime('%B')}"
    total = f"{len(rows)} goal" + ("" if len(rows) == 1 else "s")
    name = " ".join(str(quest_name or "").split()).replace('"', "'")
    of = f"the {total} in \"{name}\"" if name else f"the quest's {total}"
    head = (f"As of {when}, none of {of} is done yet" if done == 0 else
            f"As of {when}, all of {of} are done" if done == len(rows) else f"As of {when}, {done} of {of} " + ("is" if done == 1 else "are") + " done")
    finished = [" ".join(str(g.get("title") or "").split()).replace('"', "'") for g in rows if g.get("completed") and g.get("title")]
    if finished and done < len(rows):
        # What is done is named, so a status says progress, not only the backlog (measured live 2026-11-11: told to name one, the writer did not).
        head += f" (\"{finished[0]}\")" if done == 1 else f", including \"{finished[-1]}\""
    last = today + timedelta(days=OVERVIEW_WINDOW_DAYS)
    soon = sum(1 for g in open_rows if g.get("due") and today.isoformat() <= str(g.get("due"))[:10] <= last.isoformat())
    # The goals due in the coming week are counted here too: measured live 2026-11-11, a writer restated every count of the lead only to add "2 due this week".
    late_part = "none is overdue" if late == 0 else "1 is overdue" if late == 1 else f"{late} are overdue"
    soon_part = f"{soon} " + ("is" if soon == 1 else "are") + f" due by {last.day} {last.strftime('%B')}"
    tail = "" if done == len(rows) else (f", {late_part}, and {soon_part}." if soon else f", and {late_part}.")
    oldest = sorted((g for g in open_rows if days_overdue(g, today)), key=lambda g: str(g.get("due")))
    start = ""
    if oldest:
        # The longest-overdue goal is named by code too: left to the writer, a status named a later goal as "the most overdue" (measured 2026-11-11).
        g = oldest[0]
        due = date.fromisoformat(str(g.get("due"))[:10])
        # "so start there": the lead says what to do first itself, so an answer whose writer's sentences were all repeats (or were cut by a later check)
        # still says it, and a writer's "Then ..." follows something (measured live 2026-11-11: a reply that was the lead alone, and one opening "Then").
        late_by = days_overdue(g, today)
        start = f"The longest overdue is \"{g.get('title')}\", due {due.day} {due.strftime('%B')}, " + ("1 day" if late_by == 1 else f"{late_by} days") + " ago."
    else:
        upcoming = sorted((g for g in open_rows if g.get("due") and str(g.get("due"))[:10] >= today.isoformat()), key=lambda g: str(g.get("due")))
        if upcoming and tail:
            # With nothing overdue, the goal due soonest is named by code the same way: left to the writer, a status said "The first goal is due
            # 15 November" and never said which goal (measured live 2026-11-11, "Launch my pottery shop").
            g = upcoming[0]
            due = date.fromisoformat(str(g.get("due"))[:10])
            start = f"The soonest due is \"{g.get('title')}\", due {due.day} {due.strftime('%B')}."
    # ``aim`` (aim_sentence: the outcome in the person's words and a strategy with no goal) goes between the counts and the goal to start with
    return " ".join(x for x in (head + (tail or "."), aim, start) if x)


AIM_HEAD = "The quest works toward"
AIM_OUTCOME = re.compile(AIM_HEAD + r" \"([^\"]+)\"")


def aim_sentence(outcome: str, tables: Mapping[str, Any], rows: Optional[List[Mapping[str, Any]]], today: Optional[date] = None) -> str:
    """The status reply's sentence on what the quest works toward, written by code: its outcome in the person's own words and, when the plan has a
    strategy no goal is linked to, that strategy by name ('The quest works toward "I finish a half marathon in under 2:15 by 14 March", and the plan's
    strategy "Build a weekly running base" has no goal linked to it yet.'). Measured live 2026-11-11 (runs 14 to 17): asked to say progress toward the
    outcome, the writer did on some turns and on others wrote nothing past the counts, or only "the first sentence is written for you". "" without an outcome."""
    text = " ".join(str(outcome or "").split()).replace('"', "'").rstrip(".")
    if not text:
        return ""
    idle = next((" ".join(str(row.get("name")).split()) for rid, row in (tables.get("solutions") or {}).items()
                 if isinstance(row, Mapping) and str(row.get("name") or "").strip()
                 and not any(str(g.get("spec_solution_id") or "") == str(rid) for g in rows or [])), "")
    left = time_left(text, today) if today else ""
    return (f"{AIM_HEAD} \"{text}\"" + (f", {left}" if left else "")
            + (f", and the plan's strategy \"{idle}\" has no goal linked to it yet" if idle else "") + ".")


MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = "(" + "|".join(MONTH_NAMES) + ")"
OUTCOME_DAY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? " + _MONTH + r"\b(?:,? (\d{4}))?|\b" + _MONTH + r" (\d{1,2})(?:st|nd|rd|th)?\b(?:,? (\d{4}))?", re.I)


def outcome_day(outcome: str, today: date) -> Optional[date]:
    """The day an outcome names in its own words ("by 14 March", "on March 14, 2027"), or None when it names no day of a month ("by June" names none).
    A day with no year is the next one on or after ``today``."""
    m = OUTCOME_DAY.search(outcome or "")
    if not m:
        return None
    day, month, year = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
    try:
        d = date(int(year) if year else today.year, MONTH_NAMES.index(month.lower()) + 1, int(day))
    except ValueError:
        return None
    if not year and d < today:
        d = d.replace(year=today.year + 1)
    return d


def time_left(outcome: str, today: date) -> str:
    """How far off the day the outcome names is, counted by code ("17 weeks from today"), so a status says how much time the quest has without the writer
    counting: measured live 2026-11-11, "How is this quest going?" on an outcome dated 14 March never said how near it was. "" when it names no day or
    the day is past."""
    d = outcome_day(outcome, today)
    if d is None or d < today:
        return ""
    days = (d - today).days
    if days == 0:
        return "which is today"
    if days < 14:
        return "1 day from today" if days == 1 else f"{days} days from today"
    return f"{days // 7} weeks from today"


def next_step_lead(rows: List[Mapping[str, Any]], today: date, quest_name: str = "", outcome: str = "") -> str:
    """The first sentence of a reply to "What should I do next?", written by code: the ONE goal to do next and why it is first, from its dates alone
    ('In "Run my first half marathon", do "Book the race entry" next: it was due 8 November, 3 days ago, the longest overdue of its open goals.').
    Measured live 2026-11-11: "What should I do next?" and "How is this quest going?" got the same reply, word for word, on two of four accounts,
    because both opened with the goal counts. A next-step reply names no counts and no outcome: those answer how the quest is going."""
    name = " ".join(str(quest_name or "").split()).replace('"', "'")
    where = f"In \"{name}\", " if name else ""
    open_rows = [g for g in rows if not g.get("completed") and not g.get("abandoned") and g.get("title")]
    if not open_rows:
        toward = " ".join(str(outcome or "").split()).replace('"', "'").rstrip(".")
        return ((f"Every goal in \"{name}\" is done" if name else "Every goal of the quest is done")
                + (f", so the next step is to set the goal that brings \"{toward}\" closer." if toward else ", so the next step is to set its next goal."))
    overdue = sorted((g for g in open_rows if days_overdue(g, today)), key=lambda g: str(g.get("due")))
    coming = sorted((g for g in open_rows if g.get("due") and not days_overdue(g, today)), key=lambda g: str(g.get("due")))
    g = next_goal(rows, today) or open_rows[0]
    title = " ".join(str(g.get("title")).split()).replace('"', "'")
    head = (where[0].upper() + where[1:] if where else "") + (f"do \"{title}\" next" if where else f"Do \"{title}\" next")
    if overdue:
        late = days_overdue(g, today)
        why = f"it was due {said_date(g.get('due'))}, " + ("1 day" if late == 1 else f"{late} days") + " ago" + (", the longest overdue of its open goals" if len(overdue) > 1 else "")
    elif coming:
        why = f"it is due {said_date(g.get('due'))}" + (", the soonest of its open goals" if len(open_rows) > 1 else "")
    else:
        why = "none of its open goals has a due day yet"
    return f"{head}: {why}."


def aim_outcome(aim: str) -> str:
    """The outcome an ``aim_sentence`` quotes, or ""."""
    m = AIM_OUTCOME.search(aim or "")
    return m.group(1) if m else ""


def follow_sentence(rows: Optional[List[Mapping[str, Any]]], today: date) -> str:
    """The goal after the one the lead says to start with, by name with its due day ('After that, "Long run of 12 km" is due 15 November.'), in the
    lead's own order (overdue oldest first, then soonest due, then undated). It closes a status reply whose writer added nothing of its own: measured
    live 2026-11-11, "What should I do next?" came back as the counts alone on two of four accounts, and a steered rewrite returned the counts again.
    "" when there is no second open goal."""
    open_rows = [g for g in rows or [] if not g.get("completed") and not g.get("abandoned") and g.get("title")]
    overdue = sorted((g for g in open_rows if days_overdue(g, today)), key=lambda g: str(g.get("due")))
    coming = sorted((g for g in open_rows if g.get("due") and not days_overdue(g, today)), key=lambda g: str(g.get("due")))
    order = overdue + coming + [g for g in open_rows if not g.get("due")]
    if len(order) < 2:
        return ""
    g = order[1]
    title = " ".join(str(g.get("title")).split()).replace('"', "'")
    return f"After that, \"{title}\" " + (f"is due {said_date(g.get('due'))}." if g.get("due") else "has no due day yet.")


def without_closing_question(text: str) -> str:
    """A status reply's writer text (the code-written lead not in it) without the questions it ends on: the status directive says end on the last fact,
    and "Would you like me to add the proposed first goal to the quest?" ended one (measured live 2026-11-11). Punctuation only, no wording read."""
    out = (text or "").strip()
    while out.endswith("?"):
        parts = LEAD_SENTENCE_SPLIT.split(out)
        out = " ".join(parts[:-1]).strip() if len(parts) > 1 else ""
    return out


FACT_PREFIX = 4       # words are matched on their first letters ("entries" holds the "entr" of "Book the race entry")


def record_words(names: List[str]) -> List[str]:
    """The word stems of the quest's own records (its name, outcome and goal names) that a sentence must share to hold a fact about them: every word of
    five letters or more, cut to ``FACT_PREFIX`` letters."""
    return sorted({w[:FACT_PREFIX] for n in names for w in LEAD_WORD.findall(str(n or "").lower()) if len(w) >= 5})


QUOTED_NAME = re.compile(r"[\"\u201c]([^\"\u201c\u201d]{3,})[\"\u201d]")


def without_said_names(sentence: str, lead: str = "") -> str:
    """The sentence without the names the lead already quotes (quoted or bare), so what is left is what the sentence adds. Measured live 2026-11-11
    at 13:56: "Your quest "Run my first half marathon" is just getting started." passed as a fact on the quest name the lead had quoted, and took the
    place of the next goal. Compares with the lead's own text, never a list of words."""
    out = sentence or ""
    for name in sorted({m.strip() for m in QUOTED_NAME.findall(lead or "")}, key=len, reverse=True):
        out = re.sub(r"[\"\u201c]?" + re.escape(name) + r"[\"\u201d]?", " ", out, flags=re.IGNORECASE)
    return out


def holds_a_fact(sentence: str, stems: List[str], lead: str = "") -> bool:
    """True when a sentence states something from the records: a number, a quoted or bolded name, or a word of the quest's own records (``record_words``).
    Measured live 2026-11-11 (run 19): "The opening line covers the goal counts and the overdue goal, so here is what stands between the quest and its
    outcome." holds none, and said nothing about the quest. With ``lead``, a name the lead already quotes is no new fact (``without_said_names``), so a
    sentence that only adds a judgement or a made-up reason to it holds none. Compares against the quest's records, never a list of words."""
    sentence = without_said_names(sentence, lead) if lead else sentence
    if lead and LEAD_COUNTS.search(lead):
        # A lead with goal counts says the quest's figures, dates and the names it quotes: a figure or a word of those names said again is no new fact.
        # Measured live 2026-11-11 (final pass): "The race is 14 March, so every other step depends on having a confirmed entry." (an invented dependency)
        # and "Your quest ... has 2 goals open, none done yet, and 1 is overdue." held only the lead's 14, 2 and 1 and the words of "Book the race entry".
        # A no-goals lead is left out: there every record is in the lead, and a writer's first-goal proposal uses those words.
        figures = set(LEAD_NUMBER.findall(lead))
        sentence = LEAD_NUMBER.sub(lambda m: m.group(0) if m.group(0) not in figures else " ", sentence)
        said = set(record_words([m for m in QUOTED_NAME.findall(lead)]))
        stems = [s for s in stems if s not in said]
    if re.search(r"\d|[\"\u201c]|\*\*", sentence):
        return True
    return any(w[:FACT_PREFIX] in stems for w in LEAD_WORD.findall(sentence.lower()) if len(w) >= FACT_PREFIX)


SPLICED = re.compile(r",\s+([A-Z][a-z]+)\s+[a-z]")


def is_spliced(sentence: str, stems: List[str]) -> bool:
    """True when a sentence is two pieces joined mid-way: a comma, then a capitalised word that is not a word of the quest's records, then a lower-case word
    ("To build the first goal, Once you tell me which simple piece you want ...", measured live 2026-11-11 on a quest with no goals: a reply-check rewrite
    left a clause that starts a new sentence and never ends it). Letter case and the records only, no wording read."""
    return any(m.group(1).lower()[:FACT_PREFIX] not in stems for m in SPLICED.finditer(QUOTED.sub(" ", sentence or "")))


def status_rest(text: str, follow: str = "", stems: Optional[List[str]] = None, lead: str = "", always_follow: bool = False) -> str:
    """What goes after a status reply's lead: the writer's text without a closing question, a sentence that holds no fact (``holds_a_fact``, when the
    record ``stems`` are given) or a leftover fragment ("Your quest" left where the writer had copied the lead, run 19), or, when that leaves no sentence
    of four or more words, ``follow`` (follow_sentence) so the reply still says what comes next. ``lead`` is the code-written opening: a sentence
    holds a fact only beyond the names it already quotes, and a sentence that says it again is cut here too (``without_restated_lead``): the reply check
    can rewrite the writer's part after the first cut, and measured live 2026-11-11 its rewrite said the goal counts again."""
    if lead and text:
        text = without_restated_lead(text, lead)
    paragraphs = []
    for para in re.split(r"\n\s*\n", text or ""):
        p = para.strip()
        if not p:
            continue
        if not re.search(r"[.!?:][\"\u201d*)]*$", p) and len(p.split()) < 4 and not re.match(r"\s*(?:[-*\u2022]|\d+[.)])", p):
            continue        # a fragment: no end, a few words
        if stems is not None and not re.match(r"\s*(?:[-*\u2022]|\d+[.)]|\|)", p):
            p = " ".join(x for x in LEAD_SENTENCE_SPLIT.split(p) if holds_a_fact(x, stems, lead) and not is_spliced(x, stems))
        if p and follow and not re.match(r"\s*(?:[-*\u2022]|\d+[.)]|\|)", p):
            said: List[str] = []
            for x in LEAD_SENTENCE_SPLIT.split(p):
                x = as_follow(x, follow, lead)
                if x != follow or follow not in said:      # the next goal is said once, however many sentences said it
                    said.append(x)
            p = " ".join(said)
        if p:
            paragraphs.append(p)
    out = without_closing_question("\n\n".join(paragraphs))
    if not any(len(x.split()) >= 4 for x in LEAD_SENTENCE_SPLIT.split(out)):
        return follow
    named = LEAD_TITLE.search(follow or "")
    if always_follow and named and named.group(1).lower() not in out.lower():
        # "What should I do next?" closes on the goal after the one to do now, so the reply says the order, not only the first step.
        out = f"{out} {follow}".strip()
    return out


def as_follow(sentence: str, follow: str, lead: str = "") -> str:
    """``follow`` (follow_sentence) in place of a writer sentence that names the same next goal and adds nothing to it: it holds no number the follow does not,
    and at most ``REPEAT_SPARE_WORDS`` words that neither the follow nor the ``lead`` holds. Measured live 2026-11-11: 'After that, "Set up the shop page" on
    30 November.' (the writer copied the follow's shape and dropped its verb) and 'Start there, and "Choose the illustrator" follows, due 31 December.'
    (its "Start there" read as the quest named just before it). A sentence that gives a reason or a condition ("Once those photos are ready, the next one is
    ...") adds more words and stays. Compares with the code's own sentences, never a list of words."""
    m = LEAD_TITLE.search(follow or "")
    if not m or m.group(1).strip().lower() not in (sentence or "").lower():
        return sentence
    bare = QUOTED.sub(" ", sentence)
    if not set(LEAD_NUMBER.findall(bare)) <= set(LEAD_NUMBER.findall(QUOTED.sub(" ", follow))):
        return sentence
    known = set(LEAD_WORD.findall(QUOTED.sub(" ", follow).lower())) | set(LEAD_WORD.findall(QUOTED.sub(" ", lead or "").lower()))
    return follow if len(set(LEAD_WORD.findall(bare.lower())) - known) <= REPEAT_SPARE_WORDS else sentence


ASK_KIND_PROMPT = ("A person asked this about their quest:\n\n{question}\n\nDoes it ask what they should do next, first or now (NEXT), or how the quest is "
                   "going, where it stands or how far it has come (STATUS)? Answer with one word on the last line: NEXT or STATUS.")


def asks_next_step(question: str, decide: Any, reported: bool = False) -> bool:
    """Whether a status-shaped question asks what to do next (its own opening and directive) or how the quest is going, from one forced-choice cheap call.
    Measured live 2026-11-11: the request analyser's optional ``asks_next_step`` key, one of a dozen, was set for "How is this quest going?" and the
    status got the next-step reply. ``reported`` (that key) decides only when the call cannot be made or gives neither word."""
    if decide is None or not str(question or "").strip():
        return reported
    try:
        lines = [line.strip().upper() for line in str(decide(ASK_KIND_PROMPT.format(question=str(question).strip()[:600])) or "").splitlines() if line.strip()]
    except Exception as error:      # noqa: BLE001  the analyser's report stands
        log.info("next-step check not made: %s: %s", type(error).__name__, error)
        return reported
    last = re.sub(r"[^A-Z]", "", lines[-1]) if lines else ""
    return True if last == "NEXT" else False if last == "STATUS" else reported


def next_goal(rows: Optional[List[Mapping[str, Any]]], today: date) -> Optional[Mapping[str, Any]]:
    """The one open goal to do next: the longest overdue, else the soonest due, else the first open goal. None when no goal is open."""
    open_rows = [g for g in rows or [] if not g.get("completed") and not g.get("abandoned") and g.get("title")]
    overdue = sorted((g for g in open_rows if days_overdue(g, today)), key=lambda g: str(g.get("due")))
    coming = sorted((g for g in open_rows if g.get("due") and not days_overdue(g, today)), key=lambda g: str(g.get("due")))
    return (overdue or coming or open_rows or [None])[0]


# The one sentence a status or next-step reply exists for, written by one narrow model call and checked by code. Measured live 2026-11-11: told in
# a long directive to give the first action or the progress toward the outcome, the reply writer restated the counts on every account, its sentences
# were cut as repeats, and "What should I do next?" ended as the code's opening and the goal after it, with no action at all.
INSIGHT_MAX_WORDS = 40
INSIGHT_TRIES = 3       # a sentence that fails a check is asked for once more (the model's wording varies turn to turn)
NO_DASHES = "Never use an em dash or an en dash."
NEXT_ACTION_PROMPT = (
    "A person's quest is named \"{quest}\"{toward}. The goal they will do now is \"{goal}\"{due}.\n\n"
    "Write ONE sentence of at most 30 words telling them the first concrete action to take on that goal today: a specific step that does part of "
    "the goal itself, small enough to start within the hour, drawn from the goal's own words and the outcome's (never a step about the goal, such as "
    "bookmarking, planning or scheduling it). Start with a verb. Name no date, deadline or day: they were just told them. Refer to the goal by a few of its words, "
    "never its full name in quotation marks. Name no other goal. Use no number, date or price that is not in the text above, no percentage, and never "
    "say that one thing depends on, blocks or unblocks another. No question and no offer. " + NO_DASHES + " Write only the sentence.")
PROGRESS_PROMPT = (
    "A person's quest is named \"{quest}\" and works toward \"{outcome}\"{left}.\nDone goals: {done}.\nOpen goals: {open}.\n\n"
    "Write ONE sentence of at most 35 words saying how far the done goals take the quest toward that outcome and the biggest piece still missing, in "
    "plain words a coach would say. When nothing is done, say what the first real progress toward the outcome will be. Refer to goals by a few of their "
    "words, never their full names in quotation marks. They were already told the goal counts, the time left and which goal is overdue or due "
    "soonest: say none of those again, and no due dates. Use no number, date or price that is not in the text above, no percentage, and "
    "never say that one thing depends on, blocks or unblocks another. No question and no offer. " + NO_DASHES + " Write only the sentence.")
FIRST_GOAL_PROMPT = (
    "A person just started a quest named \"{quest}\"{toward}, and it has no goals yet.\n\n"
    "Propose its first goal: concrete, small enough to finish this week, drawn from the outcome's own words. Write exactly two lines and nothing else.\n"
    "Line 1: the goal, at most 10 words, starting with a verb.\n"
    "Line 2: one sentence of at most 25 words on how finishing it moves the quest toward its outcome.\n"
    "Use no number, date or price that is not in the text above, and never the words depends, blocks or unblocks. No question and no offer. " + NO_DASHES)


def checked_sentence(text: str, allowed: str, max_words: int = INSIGHT_MAX_WORDS, min_words: int = 5) -> str:
    """One model-written sentence as a reply may carry it, or "" when it fails a check: no question, percentage, dependency claim or the word
    "snapshot"; between ``min_words`` and ``max_words`` words; every number in it is in ``allowed`` (the records the prompt gave). Dashes become commas."""
    s = " ".join(str(text or "").split()).strip().strip('"\u201c\u201d').strip()
    s = re.sub(r"\s*[\u2014\u2013]\s*", ", ", s)
    why = ("empty" if not s else "a question, percentage, dependency claim or offer"
           if "?" in s or "%" in s or re.search(r"\bpercent|\bdepend|\bblock|\bunblock|\bsnapshot\b|\bI can\b|\blet me\b", s, re.I)
           else f"{len(s.split())} words" if not (min_words <= len(s.split()) <= max_words)
           else "a figure in no record" if not set(LEAD_NUMBER.findall(s)) <= set(LEAD_NUMBER.findall(allowed or "")) else "")
    if why:
        turn_log.info("Status insight sentence dropped (%s): %r", why, s[:300])
        return ""
    s = s[0].upper() + s[1:]
    return s if re.search(r"[.!][\"\u201d)]*$", s) else s + "."


def said_lines(text: str) -> List[str]:
    """A model reply's non-empty lines, list marks and "Line 1:" labels taken off."""
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)]|line \d+\s*:)\s*", "", line, flags=re.I).strip().strip("*").strip()
        if line:
            out.append(line)
    return out


def insight(decide: Any, quest_name: str, outcome: str, rows: Optional[List[Mapping[str, Any]]], today: date, next_step: bool = False) -> str:
    """The sentence a status or next-step reply exists for, from one narrow model call, checked by ``checked_sentence``: for "What should I do next?" the
    first concrete action on the goal the opening names; for "How is this quest going?" how far the done goals take the quest toward its outcome; for a
    quest with no goals, a proposed first goal in bold and how it moves the outcome. "" when there is no call to make, it fails, or its text fails a check
    (the reply then closes on the goal after, as before)."""
    if decide is None or rows is None:
        return ""
    name = " ".join(str(quest_name or "").split()).replace('"', "'")
    aim = " ".join(str(outcome or "").split()).replace('"', "'").rstrip(".")
    toward = f" and works toward \"{aim}\"" if aim else ""
    # the numbers a sentence may hold: those of the records the prompt gave (never the prompt's own word limits)
    facts = " ".join([name, aim] + [f"{g.get('title')} {said_date(g.get('due')) if g.get('due') else ''}" for g in rows or []])
    try:
        if not rows:
            if not name:
                return ""
            prompt = FIRST_GOAL_PROMPT.format(quest=name, toward=toward)
            for _ in range(INSIGHT_TRIES):
                lines = said_lines(decide(prompt))
                goal = checked_sentence(lines[-2], facts, max_words=12, min_words=2).rstrip(".") if len(lines) >= 2 else ""
                why = checked_sentence(lines[-1], facts, max_words=30) if goal else ""
                if goal and why:
                    return f"A first goal to set: **{goal}.** {why}"
            return ""
        if next_step:
            g = next_goal(rows, today)
            if g is None:
                return ""
            title = " ".join(str(g.get("title")).split()).replace('"', "'")
            due = f", due {said_date(g.get('due'))}" if g.get("due") else ""
            prompt = NEXT_ACTION_PROMPT.format(quest=name or "this quest", toward=toward, goal=title, due=due)
            for _ in range(INSIGHT_TRIES):
                got = said_lines(decide(prompt))
                # numbers only from the goal's own name ("12 km", "ten pieces"): measured live 2026-11-11, "select an event that falls before
                # 14 March" (the race IS on 14 March) was removed by the reply check and the reply was left with no action
                said = checked_sentence(without_goal_name(got[-1], title), title) if got else ""
                if said:
                    return said
            return ""
        if not aim:
            return ""
        done = [" ".join(str(g.get("title")).split()) for g in rows if g.get("completed") and g.get("title")][:6]
        open_rows = [g for g in rows if not g.get("completed") and not g.get("abandoned") and g.get("title")]
        open_rows.sort(key=lambda g: (g.get("due") is None, str(g.get("due") or "")))
        left = time_left(aim, today)
        prompt = PROGRESS_PROMPT.format(quest=name or "this quest", outcome=aim, left=f" ({left})" if left else "",
                                        done="; ".join(done) or "none yet",
                                        open="; ".join(" ".join(str(g.get("title")).split()) + (f" (due {said_date(g.get('due'))})" if g.get("due") else "")
                                                       for g in open_rows[:8]) or "none")
        for _ in range(INSIGHT_TRIES):
            got = said_lines(decide(prompt))
            said = checked_sentence(without_count_clause(got[-1], rows), facts + " " + left) if got else ""
            if said:
                return said
        return ""
    except Exception as error:      # noqa: BLE001  the sentence is an addition; the reply stands without it
        turn_log.info("Status insight not written: %s: %s", type(error).__name__, error)
        return ""


def without_count_clause(sentence: str, rows: List[Mapping[str, Any]]) -> str:
    """The progress sentence without a clause that only says the goal counts the opening gave ("None of the 2 goals in ... is done yet, and 1 is
    overdue, so the first real progress will be ...", measured live 2026-11-11 on two of four accounts): the clauses before ", so" or "; " that talk of
    goals being done or overdue with no number but the counts are cut, and what is left starts the sentence."""
    total = len(rows or [])
    done = sum(1 for g in rows or [] if g.get("completed"))
    late = sum(1 for g in rows or [] if not g.get("completed") and days_overdue(g, date.today()))
    held = {str(total), str(done), str(late), str(total - done)}
    if done == 0:
        held.add("0")
    parts = re.split(r"(,\s+so\s+|;\s+)", str(sentence or ""), maxsplit=1)
    if len(parts) == 3 and restates_goal_counts(parts[0], held):
        rest = parts[2].strip()
        return rest[0].upper() + rest[1:] if rest else sentence
    return sentence


def without_goal_name(sentence: str, title: str) -> str:
    """The action sentence without the goal's full quoted name, which the opening already said: what follows a colon after it ('Start the overdue "Book the
    race entry" goal today: open the race's registration page ...', measured live 2026-11-11), else the name with its quotation marks taken off."""
    s = str(sentence or "")
    quoted = re.search(r"[\"\u201c]" + re.escape(title) + r"[\"\u201d]", s, re.I)
    if not quoted:
        return s
    after = s[quoted.end():]
    if ":" in after:
        rest = after.split(":", 1)[1].strip()
        if len(rest.split()) >= 5:
            return rest[0].upper() + rest[1:]
    return s[:quoted.start()] + title[0].lower() + title[1:] + s[quoted.end():]


def with_insight(insight_text: str, follow: str) -> str:
    """What follows a status or next-step opening when ``insight`` wrote its sentence: that sentence, then the goal after (``follow``) unless the
    sentence already names it. The reply writer's own text is not used then: it restated the opening on every account measured (2026-11-11)."""
    named = LEAD_TITLE.search(follow or "")
    if follow and named and named.group(1).lower() in (insight_text or "").lower():
        return insight_text
    return " ".join(x for x in (insight_text, follow) if x).strip()


NO_GOALS_STEP = "A good first step is to add one small goal for this week that starts it."


def no_goals_lead(quest_name: str = "", plan: bool = False, outcome: str = "", next_step: bool = False, started: str = "",
                  today: Optional[date] = None) -> str:
    """The first sentence for a quest with no goals at all, written by code so it always names the quest: measured live 2026-11-11, "What should I do
    next?" on such a quest opened "This quest has no goals yet" and never said which quest. A plan's says that the lines under it are proposals, so no
    line has to carry the word (a writer asked to mark each one wrote "Here are three proposed first goals, each one for this period:" and
    "1. Proposal, Wednesday 11 November: ..."). "" without a name: the writer says it then."""
    name = " ".join(str(quest_name or "").split()).replace('"', "'")
    if not name:
        return ""
    # Not a plan: the sentence says a first goal follows, so the writer proposes one instead of asking the person for details (measured live
    # 2026-11-11: "What should I do next?" answered "Which piece do you want to learn, and how many days a week can you practice?").
    if plan:
        return f"\"{name}\" has no goals yet, so here are proposed first goals for this week."
    # The outcome is said by code too: "How is this quest going?" on a quest with only a name is answered by what it works toward and a first step.
    toward = " ".join(str(outcome or "").split()).replace('"', "'").rstrip(".")
    # It promises nothing the writer must deliver: measured live 2026-11-11 (run 22), "so here is a proposed first goal" stood alone when the writer's
    # step was cut. The proposal is the writer's (a first goal in bold), asked for by the status directive.
    # The first step is said by code too (run 24: the writer's own step was cut on two of three prompts, leaving only "has no goals yet"): a small goal
    # for this week is always a valid first move, and it names no fact the records do not hold.
    aim = f" It works toward \"{toward}\"." if toward else ""
    if next_step:
        return f"\"{name}\" has no goals yet, so the next step is to set its first one, small enough to finish this week." + aim
    # "How is this quest going?" says how long it has run with nothing to measure yet (the quest's own start day), then the outcome.
    day = started_day(started, today) if today else ""
    return (f"\"{name}\" was started {day} and has no goals yet, so there is no progress to measure." if day
            else f"\"{name}\" has no goals yet, so there is no progress to measure.") + aim + " " + NO_GOALS_STEP


def started_day(started: str, today: date) -> str:
    """The day a quest was started as a person says it ("today", "on 2 November"), from its record's ISO timestamp; "" when it holds none."""
    try:
        d = date.fromisoformat(str(started or "")[:10])
    except ValueError:
        return ""
    if d > today:
        return ""
    return "today" if d == today else "yesterday" if (today - d).days == 1 else f"on {d.day} {d.strftime('%B')}"


LEAD_SENTENCE_SPLIT = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"\u201d]))\s+(?=[A-Z\"\u201c])")
LEAD_NUMBER = re.compile(r"\d+")
LEAD_TITLE = re.compile(r"\"([^\"]+)\"")


LEAD_ASOF = re.compile(r"As of (\d{1,2}) ([A-Z][a-z]+)")
LEAD_COUNTS = re.compile(r"(none|all|\d+) of the (?:quest's )?(\d+) goals?")
LEAD_QUEST = re.compile(r"goals? in \"([^\"]+)\"")
LEAD_WORD = re.compile(r"[a-z0-9']+")
# A sentence is a reworded copy of the lead's count sentence when at least this share of its words are the lead's (and all its numbers are)
LEAD_COPY_SHARE = 0.8
LEAD_DUE = re.compile(r"due (\d{1,2}) ([A-Z][a-z]+)")
LEAD_OVERDUE = re.compile(r"(?:The (?:longest overdue|soonest due) is|[Dd]o) \"([^\"]+)\"")
SPELLED_VALUES = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
SPELLED_COUNT = re.compile(r"\b(" + "|".join(SPELLED_VALUES) + r")\b", re.I)
ISO_DATE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
QUOTED = re.compile(r"[\"\u201c][^\"\u201c\u201d]+[\"\u201d]")
# A sentence that names the lead's goal and holds at most this many words that are not the lead's says nothing but "do it first" (see
# without_restated_lead). A count, never a list of words: this repository does not read the model's wording against a list.
REPEAT_SPARE_WORDS = 4      # 3 kept "The first thing to do is that one." (four new words, none a fact; measured live 2026-11-11)
# A sentence about goals holding this many of the lead's figures restates its counts (see without_restated_lead)
LEAD_COUNT_FIGURES = 3


def lead_days_late(lead: str) -> set:
    """The days-late figure the lead implies (its "As of" day minus the longest-overdue goal's due day), as a set of number strings, so a sentence that only
    says "3 days late" about the goal the lead names is a restatement too. Empty when the lead holds no such pair."""
    asof, due = LEAD_ASOF.search(lead), LEAD_DUE.search(lead)
    if not asof or not due or "longest overdue" not in lead:       # a soonest-due goal is not late
        return set()
    try:
        year = date.today().year
        a = datetime.strptime(f"{asof.group(1)} {asof.group(2)} {year}", "%d %B %Y").date()
        d = datetime.strptime(f"{due.group(1)} {due.group(2)} {year}", "%d %B %Y").date()
    except ValueError:
        return set()
    if d > a:
        d = d.replace(year=year - 1)
    return {str((a - d).days)}


GOAL_STATUS_WORDS = r"\b(done|complete[d]?|overdue|late|finished|open|none)\b"


def restates_goal_counts(sentence: str, held: set) -> bool:
    """A sentence that says the goal counts again in other words ("none of its goals is done, and one is overdue"; "the quest is 0% done"): it talks of goals
    being done, complete or overdue, and every number it holds is one of the lead's (``held``; "none" counts as 0 when the lead says "none"). A percentage of
    goals is never in a snapshot, so it is dropped whatever its numbers (measured 2026-11-11: "Write a children's book is 0% done: none of its 2 goals is
    complete, and 1 is overdue" after a lead saying exactly that)."""
    if not re.search(GOAL_STATUS_WORDS, sentence, re.I):
        return False
    if "%" in sentence:
        return bool(re.search(r"\b(goals?|quest|done)\b", sentence, re.I))
    if not re.search(r"\bgoals?\b", sentence, re.I):
        return False
    numbers = set(LEAD_NUMBER.findall(QUOTED.sub(" ", sentence)))
    if re.search(r"\bnone\b", sentence, re.I):
        numbers.add("0")
    if re.search(r"\bboth\b", sentence, re.I):
        numbers.add("2")      # "Both goals ... are still open" says the total of two again (measured live 2026-11-11)
    return bool(numbers) and numbers <= held


def without_restated_lead(text: str, lead: str) -> str:
    """``text`` without the writer's sentences that only say again what the code-written ``lead`` says: every number in the sentence is one of the lead's,
    and it has two or more of them or names the goal the lead names. List lines are left alone. Measured 2026-11-11: told the first sentence was code-written,
    a writer still wrote "Your quest is at 1 of 43 goals done, with 8 overdue" after it. Pure comparison of numbers and the quoted title; no wording read."""
    held = set(LEAD_NUMBER.findall(QUOTED.sub(" ", lead or "")))      # a number inside a quoted name ("Run 5 km") is not one of the lead's figures
    counts = LEAD_COUNTS.search(lead or "")
    if counts:
        # the lead's open count is said by it too (total minus done): "3 are open" after "1 of the 4 goals is done" says nothing new (measured 2026-11-11)
        total = int(counts.group(2))
        done = 0 if counts.group(1) == "none" else total if counts.group(1) == "all" else int(counts.group(1))
        held.add(str(total - done))
    late_days = lead_days_late(lead or "")
    if re.search(r"\bnone\b", lead or "", re.I):
        held = held | {"0"}
    quest = {q.strip().lower() for q in LEAD_QUEST.findall(lead or "")}
    # the goal the lead names as longest overdue (a done goal it names is not the one "that one" may stand for)
    titles = [t.strip(" ,.").lower() for t in LEAD_OVERDUE.findall(lead or "") if t.strip().lower() not in quest]
    lead_words = set(LEAD_WORD.findall((lead or "").lower()))
    lead_own_words = set(LEAD_WORD.findall(QUOTED.sub(" ", lead or "").lower()))       # the lead's own words, not those of the names it quotes
    if not text:
        return text
    if not held:
        # The claim is the lead's first clause ("has no goals yet"); a name it quotes besides the quest (the outcome) said again goes too (run 22:
        # 'Its outcome is "I play a simple piece on the cello", and nothing has been recorded toward it so far.')
        claim = set(LEAD_WORD.findall(re.split(r"[,.;]", QUOTED.sub(" ", lead or ""))[0].lower()))
        quoted = {q.strip(" ,.").lower() for q in re.findall(r"\"([^\"]{6,})\"", lead or "")} - quest
        out = without_restated_claim(text, claim)
        return "\n".join(" ".join(x for x in LEAD_SENTENCE_SPLIT.split(line) if not any(q in x.lower() for q in quoted))
                         if not re.match(r"\s*(?:[-*\u2022]|\d+[.)]|\|)", line) else line for line in out.split("\n")).strip()
    lead_due = LEAD_DUE.search(lead or "") if titles else None
    outcome = aim_outcome(lead or "").lower()
    outcome_words = {w for w in LEAD_WORD.findall(outcome) if len(w) > 3}
    all_lead_words = set(LEAD_WORD.findall((lead or "").lower()))
    lead_quoted = {q.strip(" ,.").lower() for q in re.findall(r"\"([^\"]{6,})\"", lead or "")} - quest - set(titles)
    seen = False        # a writer sentence was kept already (see the due-day cut below)
    lines = []
    front: List[str] = []       # sentences that say "that one": they refer to the lead's goal, so they go straight after the lead (see below)
    for line in text.split("\n"):
        if re.match(r"\s*(?:[-*\u2022]|\d+[.)]|\*\*|\|)", line):
            lines.append(line)
            continue
        kept = []
        for sentence in LEAD_SENTENCE_SPLIT.split(line):
            # Numbers are read outside quoted names: "Run 5 km without stopping" is a goal's name, not a count (measured 2026-11-11: its 5 kept a sentence
            # that said the lead's counts twice over, "has 4 goals: 1 done, 3 open, and 1 overdue: 1 of its 4 goals is done ...").
            bare = ISO_DATE.sub(lambda m: f" {int(m.group(2))} ", QUOTED.sub(" ", sentence))      # "through 2026-11-18" says the lead's 18
            # A count spelled out is a count ("Three are still open, one of them overdue, and two are due by 18 November", measured live 2026-11-11);
            # "one" is left alone, it is mostly "that one" or "the one due soonest".
            bare = SPELLED_COUNT.sub(lambda m: f" {SPELLED_VALUES[m.group(1).lower()]} ", bare)
            for t in titles:
                bare = re.sub(re.escape(t), " ", bare, flags=re.I)
            numbers = set(LEAD_NUMBER.findall(bare))
            names_goal = any(t and t in sentence.lower() for t in titles)
            spare = len(set(LEAD_WORD.findall(bare.lower())) - lead_own_words)
            said = set(LEAD_WORD.findall(sentence.lower()))
            if any(q in sentence.lower() for q in lead_quoted):
                # A name the lead quotes besides the quest and the goal to start with (a done goal, the outcome, an idle strategy) said again: measured live
                # 2026-11-11 (run 18), 'The quest is at an early stage: 1 of its 4 goals is done ("Run 5 km without stopping") ...' and 'The plan's first
                # strategy, "Build a weekly running base," has no goal linked to it yet' after a lead quoting both.
                continue
            if (outcome_words and len(outcome_words & said) >= 0.6 * len(outcome_words)
                    and set(LEAD_NUMBER.findall(sentence)) <= set(LEAD_NUMBER.findall(outcome)) | held
                    and len(said - all_lead_words) <= REPEAT_SPARE_WORDS):
                # The outcome the lead quotes, said again ("The outcome is to finish a half marathon in under 2:15 by 14 March.", run 17)
                continue
            if names_goal or re.search(r",?\s+then\s+", sentence, re.I):
                # "Book the race entry first, which is 3 days overdue, then "Long run ..." and "Buy ...", both due 15 November." (measured live
                # 2026-11-11): what comes before "then" only says the lead's goal and how late it is again, so what follows it is kept on its own.
                head, then, tail = (re.split(r"(,?\s+then\s+)", sentence, maxsplit=1, flags=re.I) + ["", ""])[:3]
                head_bare = QUOTED.sub(" ", head)
                for t in titles:
                    head_bare = re.sub(re.escape(t), " ", head_bare, flags=re.I)
                # a head that does not name the goal must be the lead's own words ("Start there, then ...", run 20); one naming it may add a few
                if (then and tail.strip() and set(LEAD_NUMBER.findall(head_bare)) <= (held | late_days)
                        and len(set(LEAD_WORD.findall(head_bare.lower())) - lead_own_words) <= (REPEAT_SPARE_WORDS + 2 if names_goal else 1)
                        and not any(t and t in tail.lower() for t in titles)):
                    sentence = "After that, " + tail.strip()
                    names_goal = False
                    bare = ISO_DATE.sub(lambda m: f" {int(m.group(2))} ", QUOTED.sub(" ", sentence))
                    numbers = set(LEAD_NUMBER.findall(bare))
                    spare = len(set(LEAD_WORD.findall(bare.lower())) - lead_own_words)
            if names_goal and numbers <= (held | late_days) and spare <= REPEAT_SPARE_WORDS:
                # The lead's goal named again and nothing more ("Book the race entry first."; "Your next step is \"Book the race entry\"."; "Finish the
                # second draft."): measured live 2026-11-11 on next-step replies. The lead names it and already says to start there.
                continue
            words = set(LEAD_WORD.findall(sentence.lower()))
            restated = ((numbers and numbers <= (held | late_days if names_goal else held) and (len(numbers) >= 2 or names_goal))
                        # Three or more of the lead's figures about the goals: the counts said again, whatever else is tacked on (measured live
                        # 2026-11-11: "the quest has 4 goals: ... 4 goals, 1 done, 3 open, 1 overdue, and 2 due by 18 November, aimed at ... by 14 March").
                        or (len(numbers & held) >= LEAD_COUNT_FIGURES and re.search(r"\bgoals?\b", bare, re.I))
                        or restates_goal_counts(sentence, held)
                        # "None of your 2 goals is done, and none is overdue." after "none of the 2 goals in ... is done yet, and none is overdue." (2026-11-11)
                        or (numbers and numbers <= held and len(words) >= 5 and len(words & lead_words) >= LEAD_COPY_SHARE * len(words)))
            if restated:
                # A clause of it that holds none of the lead's figures is a new fact and stays: "Both goals in ... are still open, with one due by
                # 18 November, and photos are what you need before the shop can go live." keeps the photos (measured live 2026-11-11).
                rescued = "" if names_goal else new_fact_clauses(sentence, held)
                if rescued:
                    kept.append(rescued)
                    seen = True
                continue
            if lead_due and not seen:
                # Only the writer's first sentence: it follows the lead, so its "it" is the lead's goal. A later one may be about another goal due
                # the same day ("The most urgent item is booking the plumber. It was due 1 November ..." is a new fact).
                sentence = without_restated_due(sentence, f"{lead_due.group(1)} {lead_due.group(2)}", held | late_days, lead_own_words)
                if not sentence:
                    continue
            seen = True
            if names_goal:
                front.append(as_that_one(sentence, titles))
                continue
            kept.append(sentence)
        lines.append(" ".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if front:
        # Placed where they were, "that one" read as whatever goal came just before: measured live 2026-11-11, it followed "Done so far: Run 5 km".
        out = " ".join(front) + ("\n\n" + out if out else "")
    return out


# A clause that says the lead's due day again holds at most this many words (numbers aside) that are not the lead's own (see without_restated_due)
CLAUSE_SPARE_WORDS = 4
CLAUSE_SPLIT = re.compile(r",\s+(?=(?:and|but|so)\s)|;\s+")


def without_restated_due(sentence: str, due: str, held: set, lead_words: set) -> str:
    """``sentence`` without a clause that only says the lead's overdue goal's due day again (and how late it is): every number in it is one of the lead's
    (``held``, the days-late figure included), it holds ``due`` ("8 November"), and it adds at most ``CLAUSE_SPARE_WORDS`` words of its own. Measured live
    2026-11-11 after "The longest overdue is \"Book the race entry\", due 8 November, so start there.": "It was due 8 November and is now 3 days overdue,
    and the race is 14 March, so ..." said the date twice running; the race clause stays. "" when every clause was a repeat."""
    if due.lower() not in sentence.lower():
        return sentence
    parts = CLAUSE_SPLIT.split(sentence)
    kept = []
    for part in parts:
        bare = ISO_DATE.sub(lambda m: f" {int(m.group(2))} ", QUOTED.sub(" ", part))
        numbers = set(LEAD_NUMBER.findall(bare))
        spare = {w for w in LEAD_WORD.findall(bare.lower()) if not w.isdigit()} - lead_words
        if due.lower() in part.lower() and numbers <= held and len(spare) <= CLAUSE_SPARE_WORDS:
            continue
        kept.append(part)
    if len(kept) == len(parts):
        return sentence
    if not kept:
        return ""
    out = ", ".join(k.strip() for k in kept)
    out = re.sub(r"^(?:and|but|so)\s+", "", out.strip(), flags=re.I)
    out = out[:1].upper() + out[1:]
    return out if re.search(r"[.!?][\"\u201d]?$", out) else out.rstrip(",;") + "."


NEW_FACT_MIN_WORDS = 4


def new_fact_clauses(sentence: str, held: set) -> str:
    """The clauses of a ``sentence`` the restatement checks dropped that hold none of the lead's figures (``held``, counts spelled out included) and
    at least ``NEW_FACT_MIN_WORDS`` words, joined as a sentence; "" when the sentence is one clause or none qualifies."""
    parts = CLAUSE_SPLIT.split(sentence)
    if len(parts) < 2:
        return ""
    kept = []
    for part in parts:
        bare = SPELLED_COUNT.sub(lambda m: f" {SPELLED_VALUES[m.group(1).lower()]} ", QUOTED.sub(" ", part))
        if re.search(r"\bboth\b", bare, re.I):
            bare += " 2"
        if (set(LEAD_NUMBER.findall(bare)) & held or len(LEAD_WORD.findall(bare.lower())) < NEW_FACT_MIN_WORDS
                or re.search(GOAL_STATUS_WORDS, bare, re.I)):      # a clause about goals being done, open or late is a count fragment ("None is overdue")
            continue
        kept.append(part)
    if not kept:
        return ""
    out = re.sub(r"^(?:and|but|so)\s+", "", ", ".join(k.strip() for k in kept), flags=re.I)
    out = out[:1].upper() + out[1:]
    return out if re.search(r"[.!?][\"\u201d]?$", out) else out.rstrip(",;") + "."


def without_restated_claim(text: str, lead_words: set) -> str:
    """``text`` without the writer's sentences that hold every word of a figure-free lead (the quest has no goals): '"Learn to play the cello" has no
    goals yet.' then "This quest has no goals yet, so there is no next step on the list." (measured live 2026-11-11). The lead's own words, not those of
    the quest's quoted name, and only a lead of three or more of them. List lines are left alone."""
    if len(lead_words) < 3:
        return text
    lines = []
    for line in text.split("\n"):
        if re.match(r"\s*(?:[-*\u2022]|\d+[.)]|\*\*|\|)", line):
            lines.append(line)
            continue
        kept = []
        for sentence in LEAD_SENTENCE_SPLIT.split(line):
            if not lead_words <= set(LEAD_WORD.findall(QUOTED.sub(" ", sentence).lower())):
                kept.append(sentence)
                continue
            # Only the clause that says the claim goes: "Your first step is to pick the simple piece you want to learn first, since \"...\" has no goals
            # yet." keeps its step (measured live 2026-11-11, run 22: the whole sentence went, and with it the only proposal).
            parts = re.split(r",\s+(?=(?:and|but|so|since|because|as)\s)|;\s+", sentence)
            rest = [x for x in parts if not lead_words <= set(LEAD_WORD.findall(QUOTED.sub(" ", x).lower()))]
            # Kept only when the claim is not the sentence's first clause: "This quest has no goals yet, so there is no next step" is the claim and its
            # consequence, and goes whole.
            if rest and len(parts) > 1 and parts[0] in rest and len(" ".join(rest).split()) >= 4:
                joined = re.sub(r"^(?:and|but|so|since|because|as)\s+", "", ", ".join(r.strip() for r in rest), flags=re.I).rstrip(",;")
                kept.append(joined[:1].upper() + joined[1:] + ("" if re.search(r"[.!?][\"\u201d]?$", joined) else "."))
        lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def as_that_one(sentence: str, titles: List[str]) -> str:
    """``sentence`` with the lead's goal name (quoted or not) said as "that one", since the lead has just named it: "Book the race entry first, since
    entries close" becomes "Do that one first, since entries close". A comma or full stop the writer put inside the quotes is kept. Measured 2026-11-11:
    a next-step reply opened on the goal the lead had just named, so the same name was read in two sentences running."""
    out = sentence
    for t in titles:
        if not t:
            continue
        pattern = re.compile(r"[\"“]?" + re.escape(t) + r"([,.]?)[\"”]?", re.I)

        def say(m: "re.Match[str]", text: str = out) -> str:
            start = not text[:m.start()].strip()
            if start and re.match(r"\s*first\b", text[m.end():], re.I):
                return "Do that one" + m.group(1)
            return ("That one" if start else "that one") + m.group(1)
        out = pattern.sub(say, out)
    return out


PLAN_DAY_LINE = re.compile(r"^\s*(?:[-*•]\s*|\d+[.)]\s*)?(?:\*\*)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b")


def human_dates(text: str) -> str:
    """``text`` with every ISO date written the way a person reads it ("2026-11-08" becomes "8 November"). The snapshot lists goals by ISO due day and a
    writer copied it into replies ("3 days overdue (due 2026-11-08)", measured live 2026-11-11). A date that is not a real day is left as it is."""
    def say(m: "re.Match[str]") -> str:
        try:
            d = date.fromisoformat(m.group(0))
        except ValueError:
            return m.group(0)
        return f"{d.day} {d.strftime('%B')}"
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", say, text)


def without_plan_preamble(text: str) -> str:
    """A plan reply's ``text`` (the code-written lead not in it) without the lines before its first day line. The lead already says where things stand,
    and the plan standard asks for the day lines next; measured 2026-11-11, a writer put "Your week is short: 2 goals, 0 done and 2 still open, with 1 due
    from today through 2026-11-18. The one due soonest is ..." between them, so the counts and the goal were said twice. Line shape only (a day line opens
    on a weekday name); a text with no day line is returned as it is."""
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if PLAN_DAY_LINE.match(line)), None)
    if not first:
        return text
    return "\n".join(lines[first:]).strip()


PLAN_CLOSE = "Nothing is saved yet. Should I add this plan to your goals?"


def plan_with_days(text: str, layout: str) -> str:
    """A plan reply's text (the lead not in it), or, when it holds no day line and code laid the period out, that layout as the day lines and the plan's
    closing line. Measured live 2026-11-11 (run 19): "Plan my week" on a quest with one overdue goal came back with no day at all ("Start with ... The
    evidence shows no other items due ...") and a question about something else. Line shape only."""
    if not layout or any(PLAN_DAY_LINE.match(line) for line in (text or "").split("\n")):
        return text
    days = "\n".join(line for line in layout.split("\n") if line.startswith("- "))
    return f"{days}\n\n{PLAN_CLOSE}" if days else text


NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
LIST_LINE = re.compile(r"\s*(?:[-*•]|\d+[.)])\s+\S")
COUNT_WORD = re.compile(r"\b(" + "|".join(NUMBER_WORDS[2:]) + r"|\d+)\b(?=\s+[A-Za-z])", re.I)


def list_count_said_right(text: str) -> str:
    """``text`` with a count in a line that introduces a list (it ends with ":") made to match the list under it: "Here are three proposed first goals:"
    over one item loses "three", over two says "two". Measured live 2026-11-11: two of three proposed goals held figures, the figure cut removed them, and
    the reply still promised three. Counts list lines against a number; it reads no meaning out of the words."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.rstrip().endswith(":") or LIST_LINE.match(line):
            continue
        n = 0
        for nxt in lines[i + 1:]:
            if LIST_LINE.match(nxt):
                n += 1
            elif nxt.strip():
                break
        said = COUNT_WORD.search(line)
        if not n or not said:
            continue
        value = int(said.group(1)) if said.group(1).isdigit() else NUMBER_WORDS.index(said.group(1).lower())
        if value == n:
            continue
        word = (NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)) if n > 1 else ""
        lines[i] = re.sub(r"\s{2,}", " ", line[:said.start()] + word + line[said.end():])
    return "\n".join(lines)


def plan_lead(open_rows: List[Mapping[str, Any]], today: date, window_days: int = OVERVIEW_WINDOW_DAYS, across: int = 0, quest_name: str = "") -> str:
    """A plan reply's first sentence, written by code from the open goals: how many are overdue and how many more fall due in the period ("As of 11 November,
    8 open goals are overdue, and 1 more is due by 18 November."). ``across`` (the number of quests, for a plan over the asker's quests together) names
    the quests. Measured 2026-11-11: written by the model, the count was wrong ("Five goals are overdue" with eight laid out) or cut with the opening.
    "" when there is no open goal."""
    if not open_rows:
        return ""
    last = today + timedelta(days=max(0, window_days))
    late = sum(1 for g in open_rows if days_overdue(g, today))
    due = sum(1 for g in open_rows if g.get("due") and today.isoformat() <= str(g.get("due"))[:10] <= last.isoformat())
    name = " ".join(str(quest_name or "").split()).replace('"', "'")
    where = f" across your {across} quests" if across > 1 else (f" in \"{name}\"" if name else "")       # ``quest_name``: the plan names its quest (see lead_sentence)
    first = (f"no open goal{where} is overdue" if late == 0 else f"1 open goal{where} is overdue" if late == 1 else f"{late} open goals{where} are overdue")
    more = "more " if late else ""
    # "nothing else", not "none more" (read live 2026-11-11: "1 open goal ... is overdue, and none more is due by 18 November")
    second = (f"{'nothing else' if late else 'none'} is due by {last.day} {last.strftime('%B')}" if due == 0 else
              f"1 {more}is due by {last.day} {last.strftime('%B')}" if due == 1 else f"{due} {more}are due by {last.day} {last.strftime('%B')}")
    return f"As of {today.day} {today.strftime('%B')}, {first}, and {second}."


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


def collapse_carried(rows: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The goals as a person counts them: a goal Quest carried over to a later week is ONE goal, not one per week. Open rows with the same title (case and
    spacing aside) collapse to the one with the latest due day, which also carries ``carried_from`` (the earliest due day, when it differs). Done and set-aside
    rows, rows with no title and rows with no due day are left as they are. Measured 2026-11-11 on a four-goal quest: the status said "7 goals, 2 overdue" and
    listed "Run one tempo session per week" twice, once per week it had been carried through."""
    latest: Dict[str, Mapping[str, Any]] = {}
    earliest: Dict[str, str] = {}
    for g in rows:
        key = " ".join(str(g.get("title") or "").split()).casefold()
        due = str(g.get("due") or "")[:10]
        if g.get("completed") or g.get("abandoned") or not key or not due:
            continue
        if key not in latest or due > str(latest[key].get("due"))[:10]:
            latest[key] = g
        if key not in earliest or due < earliest[key]:
            earliest[key] = due
    out: List[Mapping[str, Any]] = []
    for g in rows:
        key = " ".join(str(g.get("title") or "").split()).casefold()
        if key in latest and not g.get("completed") and not g.get("abandoned") and str(g.get("due") or ""):
            if g is not latest[key]:
                continue
            first = earliest[key]
            out.append(dict(g, carried_from=first) if first < str(g.get("due"))[:10] else g)
        else:
            out.append(g)
    return out


def days_overdue(g: Mapping[str, Any], today: Optional[date]) -> int:
    """Whole days a goal is past its due day as of ``today`` (0 when it is not overdue, has no date, or ``today`` is not given)."""
    try:
        return max(0, (today - date.fromisoformat(str(g.get("due"))[:10])).days) if today and g.get("due") else 0
    except ValueError:
        return 0


def goal_line(g: Mapping[str, Any], today: Optional[date] = None) -> str:
    """One goal as the snapshot lists it. With ``today``, an overdue goal also carries how many days late it is: a reply that says "10 days overdue" then states
    a figure the turn read (measured 2026-11-11: computed by the writer, the figure was in no evidence and the reply check cut the whole sentence with its date)."""
    value = milestone_cue.value_line_of(str(g.get("text") or ""))
    owner = g.get("assignee_name") or ""
    late = days_overdue(g, today)
    return (f"- {g.get('title')} (due {said_date(g.get('due')) if g.get('due') else 'no date'}" + (f"; {late} days overdue" if late else "") + (f"; owner {owner}" if owner else "")
            + (f"; carried over from {g.get('carried_from')}" if g.get("carried_from") else "")
            + (f"; {value}" if value else "") + ")")


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


STRATEGY_MAX = 6


def outcome_part(outcome: str, tables: Mapping[str, Any], rows: Optional[List[Mapping[str, Any]]]) -> str:
    """What the quest works toward, so a status says progress toward it and not only the goal counts: the outcome in the person's words, and the
    strategies of the quest's plan in its order, each with how many goals are linked to it and how many of those are done. Measured live
    2026-11-11: "How is this quest going?" on a quest with an outcome dated 14 March and a strategy answered with the counts and the overdue goal
    only, never how far the quest had come toward finishing the race. "" when the quest has neither."""
    lines: List[str] = []
    text = " ".join(str(outcome or "").split())
    if text:
        lines.append(f"The outcome this quest works toward, in the person's own words: \"{text}\".")
    strategies = [(rid, row) for rid, row in (tables.get("solutions") or {}).items() if isinstance(row, Mapping) and str(row.get("name") or "").strip()]
    if strategies:
        out = []
        for rid, row in strategies[:STRATEGY_MAX]:
            linked = [g for g in rows or [] if str(g.get("spec_solution_id") or "") == str(rid)]
            done = sum(1 for g in linked if g.get("completed"))
            approach = " ".join(str(row.get("approach") or row.get("description") or "").split())[:160]
            out.append(f"- {' '.join(str(row.get('name')).split())}" + (f" ({approach})" if approach else "")
                       + (f": {len(linked)} goal" + ("" if len(linked) == 1 else "s") + f" linked, {done} done" if linked else ": no goal is linked to it yet"))
        lines.append("Strategies on the quest's plan, in its order:\n" + "\n".join(out))
    return "\n".join(lines)


def snapshot_records(quest_id: str, quest_name: str, rows: Optional[List[Mapping[str, Any]]], outcome: str = "",
                     tables: Optional[Mapping[str, Any]] = None) -> List[Dict[str, str]]:
    """The records the snapshot read, as the work trail names them (``{"ref", "label", "kind"}``): the quest by its name, its outcome in the
    person's words (kind ``outcome``), each strategy of its plan by name (kind ``strategy``, at most ``STRATEGY_MAX``, as ``outcome_part`` reads
    them), then each goal by its title. The trail shows only those the finished reply draws on. Measured live 2026-11-11: a status answer read every
    goal of the quest in code and its reply showed no "See context I used" at all; then, once it did, the panel listed the quest and its goals but
    not the outcome and strategy the answer quoted, so nothing showed which parts of the quest's structure the answer was built on."""
    out = [{"ref": str(quest_id), "label": " ".join(str(quest_name or "").split()), "kind": "quest"}] if quest_name else []
    aim = " ".join(str(outcome or "").split())
    if aim:
        out.append({"ref": f"{quest_id}#outcome", "label": aim, "kind": "outcome"})
    strategies = [(rid, row) for rid, row in ((tables or {}).get("solutions") or {}).items() if isinstance(row, Mapping) and str(row.get("name") or "").strip()]
    for rid, row in strategies[:STRATEGY_MAX]:
        out.append({"ref": str(rid), "label": " ".join(str(row.get("name")).split()), "kind": "strategy"})
    for g in rows or []:
        if g.get("id") and g.get("title"):
            out.append({"ref": str(g.get("id")), "label": " ".join(str(g.get("title")).split()), "kind": "goal"})
    return out


def build(registry: Any, quest_id: str, asker_user_id: str = "", own: bool = False, question: str = "", decide: Any = None,
          window_days: int = OVERVIEW_WINDOW_DAYS, plan: bool = False, today: Optional[date] = None, quest_title: str = "",
          quest_name: str = "", quest_outcome: str = "", next_step: bool = False, started: str = "") -> Optional[Dict[str, Any]]:
    """The snapshot as one gathered read (locator ``status_snapshot``), or None when no part could be read. With ``question`` and ``decide``, a question about one
    milestone also gets that milestone's records (what blocks it, who owns it, its next step and date). ``plan`` (a request to lay out a coming period, ``window_days``
    long) adds the open goals with no date to the goals at a glance. ``quest_title`` (its name and outcome, ``quest_label``) heads the snapshot, so the reply
    names the quest by name: without it a quest with no name was written as its id (measured 2026-11-11). Never raises."""
    if registry is None or not quest_id:
        return None
    try:
        today = today or date.today()
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
            parts.append("Active priorities, most important first:\n" + "\n".join(goal_line(g, today) for g in active))
        everything = all_goal_rows(registry, quest_id)
        if everything is not None:
            everything = collapse_carried(everything)
        lead = ""
        aim = outcome_part(quest_outcome, tables, everything)
        if aim:
            parts.append(aim)
        said_aim = aim_sentence(quest_outcome, tables, everything, today) if not plan else ""
        if everything is not None:
            parts.append(overview(list(everything), today, window_days, plan=plan))
            open_rows = [g for g in everything if not g.get("completed") and not g.get("abandoned")]
            lead = ((plan_lead(open_rows, today, window_days, quest_name=quest_name) if everything else no_goals_lead(quest_name, plan=True)) if plan
                    else lead_sentence(list(everything), today, quest_name=quest_name, aim=said_aim, next_step=next_step, started=started))
        if own and asker_user_id:
            mine = [g for g in goal_rows(registry, quest_id, open_only=True, assignee_user_id=asker_user_id) if not g.get("completed") and not g.get("abandoned")][:OWN_MAX]
            if mine:
                parts.append("The asker's own open goals on this quest, soonest due first (overdue ones lead):\n" + "\n".join(goal_line(g, today) for g in mine))
        if not parts:
            return None
        # The name apart from the outcome: given "Learn to play the cello (I play a simple piece on the cello)" as "this name", a reply called the quest
        # by its outcome (measured live 2026-11-11).
        name = " ".join(str(quest_name or "").split())
        named = (f"The quest is named \"{name}\" (name and outcome: {quest_title.strip() or name}). Call it \"{name}\", never by its outcome or its "
                 "id.\n\n" if name else f"The quest: {quest_title.strip()} (call it by this name; never write its id).\n\n" if quest_title.strip() else "")
        # Said as what the person was already told, not as "an opening written by code": told about an opening, writers wrote about it ("The opening
        # line above already names your next step", "The first sentence is written for you", measured live 2026-11-11).
        opening = (f"THE PERSON HAS ALREADY BEEN TOLD, word for word: \"{lead}\" Continue straight on from it. Never repeat it or any of its counts, "
                   "the outcome it quotes, the strategy it names, the goal it names, or that the quest has no goals, and never mention that "
                   "anything was said before.\n\n" if lead else "")
        said = insight(decide, quest_name, quest_outcome, list(everything) if everything is not None else None, today, next_step=next_step) if not plan else ""
        if said and not everything:
            lead = lead.replace(" " + NO_GOALS_STEP, "")      # the proposed first goal says it, concretely
        asked = ("THE PERSON ASKED WHAT TO DO NEXT: the goal to do now is the one they were told. Say how to start it today, not how the quest is going.\n\n"
                 if next_step and not plan else "")
        return {"kind": "query", "locator": LOCATOR, "text": (PLAN_HEAD if plan else HEAD) + ":\n\n" + named + opening + asked + "\n\n".join(parts), "lead": lead,
                "next_step": bool(next_step and not plan), "insight": said,
                "follow": "" if plan else follow_sentence(everything, today), "records": snapshot_records(quest_id, quest_name, everything, quest_outcome, tables),
                "stems": record_words([quest_name, quest_outcome] + [g.get("title") for g in everything or []]),
                "layout": day_layout([g for g in everything or [] if not g.get("completed") and not g.get("abandoned")], today, window_days) if plan else ""}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("status snapshot not built: %s: %s", type(error).__name__, error)
        return None


def portfolio(registry: Any, quests: List[Mapping[str, str]], plan: bool = False, window_days: int = OVERVIEW_WINDOW_DAYS) -> Optional[Dict[str, Any]]:
    """The asker's quests together as one gathered read (locator ``status_snapshot``): per quest (at most ``PORTFOLIO_QUESTS``) its outcome and its open goals, soonest due
    first (at most ``PORTFOLIO_GOALS``), each with its due day, owner and value line. ``quests`` is ``reachable_quests``. None when no quest has an open goal to show.
    With ``plan`` (a request to lay out a coming period in a chat with no quest open) it is a planning snapshot that also lists the days of the period.
    Never raises."""
    if registry is None or not quests:
        return None
    try:
        blocks: List[str] = []
        today = date.today()
        every: List[Mapping[str, Any]] = []      # every quest's open goals, each title carrying its quest, for a plan's day layout
        for quest in quests[:PORTFOLIO_QUESTS]:
            quest_id = str(quest.get("quest_id") or "")
            if not quest_id:
                continue
            rows = [g for g in goal_rows(registry, quest_id, open_only=True) if not g.get("completed") and not g.get("abandoned")]
            rows.sort(key=lambda g: (g.get("due") is None, str(g.get("due") or "")))
            title = str(quest.get("title") or "").strip() or "Untitled quest"
            every.extend(dict(g, title=f"{g.get('title')} [{title}]") for g in rows)
            if rows:
                # The quest's own counts, dated, so a reply that says "7 overdue as of 11 November" states what the turn read (measured 2026-11-11: with no
                # counts the writer counted for itself, and the date check cut the sentence and the goals' dates with it).
                late = sum(1 for g in rows if days_overdue(g, today))
                counts = f"{len(rows)} open goal" + ("" if len(rows) == 1 else "s") + f" as of {today.isoformat()}, {late} of them overdue."
                blocks.append(f"Quest: {title}\n{counts}\n" + "\n".join(goal_line(g, today) for g in rows[:PORTFOLIO_GOALS]) + (f"\n(and {len(rows) - PORTFOLIO_GOALS} more open goals)" if len(rows) > PORTFOLIO_GOALS else ""))
            else:
                blocks.append(f"Quest: {title}\nNo open goals.")
        if not any("No open goals." not in b for b in blocks):
            return None
        lead = ""
        if plan:
            lead = plan_lead(every, today, window_days, across=len({str(g.get("title")).rsplit(" [", 1)[-1] for g in every}))
            days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
            blocks.append(f"Today is {today.isoformat()}. The days of this period: " + ", ".join(said_day(d) for d in days) + ".")
            # Measured 2026-11-11: shown six goals a quest, the writer said it could "only see the first six" and laid out no week. The layout is placed by
            # code from every open goal of every quest, the quest in brackets after each goal.
            layout = day_layout(every, today, window_days)
            if layout:
                blocks.append("Day layout across these quests, worked out by code from all their open goals (every overdue goal and every goal due in the period "
                              f"has a day, on or before its due day, at most {PLAN_PER_DAY} a day while there is room; the quest is in brackets after each goal; "
                              "write the plan from this layout, keeping each goal on its day and naming its quest):\n" + layout)
        else:
            blocks.append(f"Today is {today.isoformat()}.")
        opening = (f"THE PERSON HAS ALREADY BEEN TOLD, word for word: \"{lead}\" Continue straight on from it: never repeat its counts, and never "
                   "mention that anything was said before.\n\n" if lead else "")
        return {"kind": "query", "locator": LOCATOR, "text": (PORTFOLIO_PLAN_HEAD if plan else PORTFOLIO_HEAD) + ":\n\n" + opening + "\n\n".join(blocks), "lead": lead}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("portfolio snapshot not built: %s: %s", type(error).__name__, error)
        return None
