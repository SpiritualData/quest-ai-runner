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
    return [{"quest_id": str(r.get("quest_id") or r.get("id")), "title": quest_label(r), "name": str(r.get("name") or "").strip(), "state": ""}
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
                "that failed, and never ask to read it again. Then write out, in this reply, up to three proposed first goals that move the quest's outcome "
                "(one line each, each marked as a proposal and given a day of this period, the first for today or tomorrow): never only offer to propose them.\n"
                "The days of this period: " + ", ".join(f"{d.strftime('%A')} {d.isoformat()}" for d in days) + ".")
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
    if plan:
        # The days of the period, so a plan's day lines ("Thursday 12 November") rest on a date the turn holds: the reply check drops a clause whose date
        # is in nothing read, and before this it cut every day label out of the plan and left fragments.
        days = [today + timedelta(days=n) for n in range(max(0, window_days) + 1)]
        lines.append("The days of this period: " + ", ".join(f"{d.strftime('%A')} {d.isoformat()}" for d in days) + ".")
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
        placed[room].append(f"{g.get('title')} (" + (f"overdue, due {g.get('due')}" if late else f"due {g.get('due')}")
                            + (f"; carried over from {g.get('carried_from')}" if g.get("carried_from") else "") + ")")
    for g in undated:
        empty = next((d for d in days if not placed[d]), None)
        if empty is None:
            break
        placed[empty].append(f"{g.get('title')} (no date)")
    if not any(placed.values()):
        return ""
    out = [f"- {d.strftime('%A')} {d.isoformat()}: " + "; ".join(placed[d]) for d in days if placed[d]]
    if unplaced:
        out.append(f"(and {len(unplaced)} more overdue goals with no room this period: " + "; ".join(str(g.get("title")) for g in unplaced[:OVERVIEW_MAX]) + ")")
    return "\n".join(out)


def lead_sentence(rows: List[Mapping[str, Any]], today: date, quest_name: str = "") -> str:
    """The status reply's first sentence, written by code from the goal counts ("As of 11 November, none of the quest's 25 goals is done yet, and 7 are
    overdue."), so the overall picture is always stated and stated right. "" for a quest with no goals (the reply says that itself). ``quest_name`` names
    the quest in it ("1 of the 4 goals in \"Run my first half marathon\" is done"): measured 2026-11-11, a reply that opened "the quest's 2 goals" never
    named the quest at all on two of four test accounts, and one called its outcome "The quest 'I sell 20 pieces online'"."""
    if not rows:
        return ""
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
    if oldest:
        # The longest-overdue goal is named by code too: left to the writer, a status named a later goal as "the most overdue" (measured 2026-11-11).
        g = oldest[0]
        due = date.fromisoformat(str(g.get("due"))[:10])
        # "so start there": the lead says what to do first itself, so an answer whose writer's sentences were all repeats (or were cut by a later check)
        # still says it, and a writer's "Then ..." follows something (measured live 2026-11-11: a reply that was the lead alone, and one opening "Then").
        tail = tail[:-1] + f". The longest overdue is \"{g.get('title')}\", due {due.day} {due.strftime('%B')}, so start there."
    return head + (tail or ".")


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
LEAD_OVERDUE = re.compile(r"The longest overdue is \"([^\"]+)\"")
SPELLED_VALUES = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
SPELLED_COUNT = re.compile(r"\b(" + "|".join(SPELLED_VALUES) + r")\b", re.I)
ISO_DATE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
QUOTED = re.compile(r"[\"\u201c][^\"\u201c\u201d]+[\"\u201d]")
# A sentence that names the lead's goal and holds at most this many words that are not the lead's says nothing but "do it first" (see
# without_restated_lead). A count, never a list of words: this repository does not read the model's wording against a list.
REPEAT_SPARE_WORDS = 3
# A sentence about goals holding this many of the lead's figures restates its counts (see without_restated_lead)
LEAD_COUNT_FIGURES = 3


def lead_days_late(lead: str) -> set:
    """The days-late figure the lead implies (its "As of" day minus the longest-overdue goal's due day), as a set of number strings, so a sentence that only
    says "3 days late" about the goal the lead names is a restatement too. Empty when the lead holds no such pair."""
    asof, due = LEAD_ASOF.search(lead), LEAD_DUE.search(lead)
    if not asof or not due:
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


def restates_goal_counts(sentence: str, held: set) -> bool:
    """A sentence that says the goal counts again in other words ("none of its goals is done, and one is overdue"; "the quest is 0% done"): it talks of goals
    being done, complete or overdue, and every number it holds is one of the lead's (``held``; "none" counts as 0 when the lead says "none"). A percentage of
    goals is never in a snapshot, so it is dropped whatever its numbers (measured 2026-11-11: "Write a children's book is 0% done: none of its 2 goals is
    complete, and 1 is overdue" after a lead saying exactly that)."""
    if not re.search(r"\b(done|complete[d]?|overdue|late|finished)\b", sentence, re.I):
        return False
    if "%" in sentence:
        return bool(re.search(r"\b(goals?|quest|done)\b", sentence, re.I))
    if not re.search(r"\bgoals?\b", sentence, re.I):
        return False
    numbers = set(LEAD_NUMBER.findall(sentence))
    if re.search(r"\bnone\b", sentence, re.I):
        numbers.add("0")
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
    if not held or not text:
        return text
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
            if names_goal and numbers <= (held | late_days) and spare <= REPEAT_SPARE_WORDS:
                # The lead's goal named again and nothing more ("Book the race entry first."; "Your next step is \"Book the race entry\"."; "Finish the
                # second draft."): measured live 2026-11-11 on next-step replies. The lead names it and already says to start there.
                continue
            if numbers and numbers <= (held | late_days if names_goal else held) and (len(numbers) >= 2 or names_goal):
                continue
            if len(numbers & held) >= LEAD_COUNT_FIGURES and re.search(r"\bgoals?\b", bare, re.I):
                # Three or more of the lead's figures about the goals: the counts said again, whatever else is tacked on (measured live 2026-11-11:
                # "the quest has 4 goals: ... 4 goals, 1 done, 3 open, 1 overdue, and 2 due by 18 November, aimed at ... by 14 March").
                continue
            if restates_goal_counts(sentence, held):
                continue
            words = set(LEAD_WORD.findall(sentence.lower()))
            if numbers and numbers <= held and len(words) >= 5 and len(words & lead_words) >= LEAD_COPY_SHARE * len(words):
                # "None of your 2 goals is done, and none is overdue." after "none of the 2 goals in ... is done yet, and none is overdue." (2026-11-11)
                continue
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


PLAN_DAY_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b")


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
    second = (f"none {'more ' if late else ''}is due by {last.day} {last.strftime('%B')}" if due == 0 else
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
    return (f"- {g.get('title')} (due {g.get('due') or 'no date'}" + (f"; {late} days overdue" if late else "") + (f"; owner {owner}" if owner else "")
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


def snapshot_records(quest_id: str, quest_name: str, rows: Optional[List[Mapping[str, Any]]]) -> List[Dict[str, str]]:
    """The records the snapshot read, as the work trail names them (``{"ref", "label", "kind"}``): the quest by its name, then each goal by its title.
    The trail shows only those the finished reply draws on. Measured live 2026-11-11: a status answer read every goal of the quest in code and its
    reply showed no "See context I used" at all, so the person could not see what it was based on."""
    out = [{"ref": str(quest_id), "label": " ".join(str(quest_name or "").split()), "kind": "quest"}] if quest_name else []
    for g in rows or []:
        if g.get("id") and g.get("title"):
            out.append({"ref": str(g.get("id")), "label": " ".join(str(g.get("title")).split()), "kind": "goal"})
    return out


def build(registry: Any, quest_id: str, asker_user_id: str = "", own: bool = False, question: str = "", decide: Any = None,
          window_days: int = OVERVIEW_WINDOW_DAYS, plan: bool = False, today: Optional[date] = None, quest_title: str = "",
          quest_name: str = "") -> Optional[Dict[str, Any]]:
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
        if everything is not None:
            parts.append(overview(list(everything), today, window_days, plan=plan))
            open_rows = [g for g in everything if not g.get("completed") and not g.get("abandoned")]
            lead = plan_lead(open_rows, today, window_days, quest_name=quest_name) if plan else lead_sentence(list(everything), today, quest_name=quest_name)
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
        opening = (f"The reply's first sentence is written by code and goes before your text, word for word: \"{lead}\" Do not write it, and do not restate "
                   "its counts or the goal it names.\n\n" if lead else "")
        return {"kind": "query", "locator": LOCATOR, "text": (PLAN_HEAD if plan else HEAD) + ":\n\n" + named + opening + "\n\n".join(parts), "lead": lead,
                "records": snapshot_records(quest_id, quest_name, everything)}
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
            blocks.append(f"Today is {today.isoformat()}. The days of this period: " + ", ".join(f"{d.strftime('%A')} {d.isoformat()}" for d in days) + ".")
            # Measured 2026-11-11: shown six goals a quest, the writer said it could "only see the first six" and laid out no week. The layout is placed by
            # code from every open goal of every quest, the quest in brackets after each goal.
            layout = day_layout(every, today, window_days)
            if layout:
                blocks.append("Day layout across these quests, worked out by code from all their open goals (every overdue goal and every goal due in the period "
                              f"has a day, on or before its due day, at most {PLAN_PER_DAY} a day while there is room; the quest is in brackets after each goal; "
                              "write the plan from this layout, keeping each goal on its day and naming its quest):\n" + layout)
        else:
            blocks.append(f"Today is {today.isoformat()}.")
        opening = (f"The reply's first sentence is written by code and goes before your text, word for word: \"{lead}\" Do not write it, and do not restate "
                   "its counts.\n\n" if lead else "")
        return {"kind": "query", "locator": LOCATOR, "text": (PORTFOLIO_PLAN_HEAD if plan else PORTFOLIO_HEAD) + ":\n\n" + opening + "\n\n".join(blocks), "lead": lead}
    except Exception as error:      # noqa: BLE001  a snapshot is an addition; its failure never stops the turn
        log.info("portfolio snapshot not built: %s: %s", type(error).__name__, error)
        return None
