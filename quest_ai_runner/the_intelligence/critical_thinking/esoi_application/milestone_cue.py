"""Which milestone of a quest a short cue concerns, found by code before the planner plans, and the keyed step recorded by code once it is found.

Role: an arm of the Spec Selection idea (choose, from an ESOI specification, the few records a request concerns) for the records a quest keeps
in Quest, and the code-driven flow step of ESOI Application that follows: read the records, write the owned next step under a key that repeats safely,
return the receipt. A cue such as "get the release moving" names no record; the quest's solutions (the strategies, each with its milestones and
metric) are scored against it by the words they share, weighted by how rare each word is among them, and the best is handed to the planner as a block
of facts: the solution id, what done means, the metric and its current value, the owners the record lists, the goals already linked to it, and the
increment order. Nothing here reads the model's output. Resolving a milestone reads; ``Resolved.carry_out`` is the one write, and it runs when the
planner's own structured report says the person asked for something to be done and the plan has not chosen the higher rungs of the increment order
(a deliverable written in the reply, a metric value recorded by a tool). A failure to read or write is a ``LaneGap``, never an empty result. When two
solutions are close the one the quest lists first wins: a quest lists its solutions in the order its owners ranked them.
"""
from __future__ import annotations

import datetime
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

STOP = frozenset("""a about all also an and any are as at be been but by can could do does for from get go going had has have how i in into is it its let lets
make me more my need new next not now of on one or our out please so some than that the their them then there these they this to up us want was we were what
when which who will with would you your""".split())
NAME_WEIGHT, METRIC_WEIGHT, TEXT_WEIGHT = 3.0, 2.0, 1.0
MIN_SCORE = 2.0
CLOSE = 0.85           # a runner-up at this share of the best score counts as a tie
VALUE_CHARS = 220
ACT = "act"
CODE_ROUTED_ACTIONS = ("answer", "deep", "confirm", "clarify")    # a plan that, for an act cue, the flow step takes over
DELIVERABLE_TARGET = "draft_in_chat"                              # the planner reports a deliverable written in the reply: the first rung of the increment order


class LaneGap(RuntimeError):
    """The lane could not read or write what the milestone flow needs. It is raised, never swallowed: the reply is told what is missing."""


def words(text: Any) -> List[str]:
    out = []
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        token = token[:-1] if len(token) > 3 and token.endswith("s") else token
        if token not in STOP and len(token) > 2:
            out.append(token)
    return out


def metrics_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], solution_id: str) -> List[Tuple[str, Mapping[str, Any]]]:
    """The metrics that measure a solution, from the metric records' own ``solution_ids``."""
    return [(mid, m) for mid, m in (tables.get("metrics") or {}).items() if solution_id in (m.get("solution_ids") or [])]


def solutions_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> List[Tuple[str, Mapping[str, Any]]]:
    """The solutions that are milestones of the quest (they state what done means and are measured), in the order the quest lists them."""
    return [(rid, row) for rid, row in (tables.get("solutions") or {}).items()
            if str(row.get("milestones") or "").strip() and (metrics_of(tables, rid) or row.get("measures_of_success"))]


def index_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    """A specification index of the quest's milestones in the shape ``spec-selection`` reads: one ``solution`` entry per milestone (its name as the
    title, its milestone, purpose and measures as the summary, and the names of the metrics that measure it)."""
    entries = []
    for rid, row in solutions_of(tables):
        linked = metrics_of(tables, rid)
        entries.append({"id": rid, "kind": "solution", "title": str(row.get("name") or ""),
                        "summary": " ".join(str(row.get(f) or "") for f in ("milestones", "purpose", "measures_of_success", "outputs")),
                        "metric_title": " ".join(str(m.get("name") or "") for _, m in linked)})
    return {"version": 1, "entries": entries}


def rank(index: Mapping[str, Any], cue: str) -> List[Tuple[float, Mapping[str, Any]]]:
    """``(score, entry)`` for the entries the cue fits well enough, in the quest's own order among near-equals: the best, then every entry within ``CLOSE`` of it."""
    entries = list(index.get("entries") or [])
    docs = [{"name": set(words(e.get("title"))), "metric": set(words(e.get("metric_title"))), "text": set(words(e.get("summary")))} for e in entries]
    wanted = set(words(cue))
    containing = {w: sum(1 for d in docs if any(w in part for part in d.values())) for w in wanted}
    scores = []
    for entry, doc in zip(entries, docs):
        score = 0.0
        for w in wanted:
            if containing.get(w):
                idf = math.log(1 + len(docs) / containing[w])
                score += idf * max(NAME_WEIGHT * (w in doc["name"]), METRIC_WEIGHT * (w in doc["metric"]), TEXT_WEIGHT * (w in doc["text"]))
        scores.append((score, entry))
    best = max((sc for sc, _ in scores), default=0.0)
    if best < MIN_SCORE:
        return []
    return [(sc, e) for sc, e in scores if sc >= CLOSE * best]


def select(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str) -> Optional[Tuple[str, Mapping[str, Any], List[str]]]:
    """``(solution id, its row, ids of the close runners-up)`` for the milestone the cue concerns, chosen by the ``spec-selection`` arm for a quest's records
    (``quest-records-idf``), else None when nothing fits well enough."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.spec_selection import quest_records_idf
    chosen = quest_records_idf(cue, index_of(tables)).ids
    if not chosen:
        return None
    solutions = dict(solutions_of(tables))
    return chosen[0], solutions[chosen[0]], chosen[1:]


def first_sentence(text: str, limit: int = 160) -> str:
    """The start of a recorded value, cut at a sentence end or the limit: enough to say where the measure stands."""
    text = " ".join(text.split())
    cut = re.search(r"[.;](\s|$)", text[:limit])
    return (text[:cut.start()] if cut else text[:limit]).strip()


def owners_of(row: Mapping[str, Any]) -> List[str]:
    """The people the milestone's record lists as responsible, from the record's own ``tracking_owner`` column, separated by ``;`` or ``,``."""
    return [part.strip() for part in re.split(r"[;,]", str(row.get("tracking_owner") or "")) if part.strip()]


@dataclass(frozen=True)
class Week:
    """The week a cue is acted in, read in the quest's own time zone: its Monday, its period key and the day its step is due (the week's last day)."""
    monday: str
    period: str
    due: str
    zone: str = ""

    @staticmethod
    def of(day: datetime.date, zone: str = "") -> "Week":
        monday = day - datetime.timedelta(days=day.weekday())
        iso = day.isocalendar()
        return Week(monday.isoformat(), f"{iso[0]}_W{iso[1]:02d}", (monday + datetime.timedelta(days=6)).isoformat(), zone)


def today_in(zone: str, now: Optional[datetime.datetime] = None) -> datetime.date:
    """The calendar day it is in ``zone`` (an IANA name); UTC when the quest records none."""
    from zoneinfo import ZoneInfo
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        return moment.astimezone(ZoneInfo(zone) if zone else datetime.timezone.utc).date()
    except Exception as error:      # noqa: BLE001  an unknown zone name is a gap in the quest's settings, said plainly
        raise LaneGap(f"the quest's time zone {zone!r} is not a zone this lane knows ({type(error).__name__}), so the week could not be dated") from error


def next_step_title(rid: str, week: Week) -> str:
    return f"{rid} next step: week of {week.monday}"


def linked_goals(goals: Sequence[Mapping[str, Any]], rid: str) -> List[Mapping[str, Any]]:
    """The goals already on the quest for this milestone: linked to its solution record, or titled with its record id (the key every goal of it carries)."""
    return [g for g in goals if str(g.get("spec_solution_id") or "") == rid or str(g.get("title") or "").startswith(rid + " ")]


def goal_summary(goal: Mapping[str, Any]) -> str:
    state = "done" if goal.get("completed") else ("abandoned" if goal.get("abandoned") else "open")
    return f"{goal.get('title')!r} [{state}, due {goal.get('due') or 'no date'}, {goal.get('assignee_name') or goal.get('assignee_user_id') or 'unassigned'}]"


INCREMENT_ORDER = ("Increment order, the first that applies (one increment per turn): "
                   "1. a deliverable the milestone needs that you can write from what you have: write it in the reply (deep_target draft_in_chat) and record it as a goal keyed "
                   "{rid} followed by a short name; "
                   "2. else a metric value you can read from the data your tools reach: record it with the metric's record id and its source and date; "
                   "3. else an owned next step that names what only a person can do. Code records that step for you under its key and returns the receipt, "
                   "so do not choose a deep run, do not call create_or_update_quest_goal for it, and never ask permission to do it.")


def fact_block(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, monday: str = "", goals: Sequence[Mapping[str, Any]] = (),
               zone: str = "") -> str:
    """The facts for the planner, or "" when no milestone fits the cue."""
    picked = select(tables, cue)
    if picked is None:
        return ""
    rid, row, ties = picked
    linked = metrics_of(tables, rid)
    metric_ids = [mid for mid, _ in linked]
    metric = linked[0][1] if linked else None
    name = str(row.get("name") or rid)
    owners = owners_of(row)
    value = first_sentence(str(metric.get("record") or "")) if metric else ""
    lines = [f"CUE FACTS (found by code from the quest's own records, not by you). The cue concerns the milestone {name!r}, solution {rid}.",
             f"- Milestone: {name} | Done when: {str(row.get('milestones') or '').strip()[:VALUE_CHARS].rstrip('.')} | Current value: {value or 'not recorded yet'}."]
    if metric:
        lines.append(f"- Metric record {metric_ids[0]} ({metric.get('name')}); its full recorded text is in read_spec_records.")
    lines.append("- Owners the record lists: " + (", ".join(owners) if owners else "none recorded on the milestone, so say the step has no recorded owner and name no one"))
    if ties:
        lines.append("- Close runners-up (the quest lists this one first): " + ", ".join(f"{t} {str((tables['solutions'].get(t) or {}).get('name') or '')[:50]!r}" for t in ties))
    already = linked_goals(goals, rid)
    lines.append("- Goals already on the quest for this milestone: " + ("; ".join(goal_summary(g) for g in already) if already
                 else "none") + (". Say 'already there' for a step or deliverable listed here and do not create it again." if already else "."))
    lines.append(f"- Keys: this week's next step is the goal titled '{rid} next step: week of {monday or '<Monday date>'}'"
                 + (f" (week read in {zone})" if zone else " (the quest records no time zone, so UTC)") + f", linked to spec_solution_id {rid}"
                 + (f" and spec_metric_id {metric_ids[0]}" if metric_ids else "") + f"; a deliverable is a goal titled '{rid} <short name>'. The tool reports created, updated or already there.")
    lines.append(INCREMENT_ORDER.replace("{rid}", rid))
    lines.append("Use them: do not ask which milestone, and do not ask for facts listed here.")
    return "\n".join(lines)


@dataclass
class Resolved:
    """The milestone a cue concerns, with what the flow step needs to write its keyed next step."""
    rid: str
    name: str
    row: Mapping[str, Any]
    metric_ids: List[str]
    owners: List[str]
    week: Week
    facts: str
    quest_id: str
    done_when: str = ""
    value: str = ""
    receipt: str = field(default="")

    def routes_to_code(self, plan: Any, user_intent: Optional[str], gathered: Sequence[Mapping[str, Any]], registry: Any) -> bool:
        """True when the increment is the third rung: the person asked for something to be done (the planner's ``user_intent``), no deliverable is being written in the
        reply (``deep_target``), no tool has already changed something this turn, and the plan is one the flow step takes over. Structured fields only."""
        from quest_ai_runner.the_world.tools import tool_guards
        return (not self.receipt and user_intent == ACT and getattr(plan, "action", None) in CODE_ROUTED_ACTIONS
                and getattr(plan, "deep_target", None) != DELIVERABLE_TARGET and not tool_guards.mutating_tool_ran(gathered, registry))

    def goal_arguments(self) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "quest_id": self.quest_id, "title": next_step_title(self.rid, self.week), "period": self.week.period, "due_date": self.week.due,
            "description": f"Next step for {self.name}. Done when: {self.done_when}. Current value: {self.value or 'not recorded yet'}.",
            "spec_solution_id": self.rid, "user_asked_for_this_field": True}
        if self.metric_ids:
            args["spec_metric_id"] = self.metric_ids[0]
        return args

    def carry_out(self, registry: Any) -> Tuple[str, str]:
        """The keyed idempotent write, then its receipt: ``(receipt text, the tool's typed status)``. Each listed owner is tried as the assignee in turn; with none that Quest accepts
        the step is written unassigned and the receipt says so. A tool the lane lacks, or a write that failed or is held, is a ``LaneGap`` carrying the tool's own words."""
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if registry is None or registry.get("create_or_update_quest_goal") is None:
            raise LaneGap("this lane has no create_or_update_quest_goal tool, so the next step could not be recorded")
        ctx = ToolContext(quest_id=self.quest_id)
        base = self.goal_arguments()
        refused: List[str] = []
        for owner in [*self.owners, ""]:
            args = dict(base, assignee=owner) if owner else dict(base)
            try:
                result = registry.invoke("create_or_update_quest_goal", args, ctx)
            except Exception as error:      # noqa: BLE001  surfaced as a gap, with the cause
                raise LaneGap(f"recording the next step failed ({type(error).__name__}: {error})") from error
            if result.ok:
                self.receipt = result.text
                note = f" The owner could not be set ({'; '.join(refused)})." if refused and not owner else ""
                return (f"RECEIPT written by code for {self.name!r}: {result.text}{note} It is recorded: say so as done, say who owns it and what only a person can do, "
                        "and do not offer to do it or ask whether to."), result.status.value
            if owner and "assignee" in str(result.text):
                refused.append(f"{owner}: {str(result.text).strip()[:160]}")
                continue
            raise LaneGap(f"the next step for {self.name!r} was not recorded: {str(result.text).strip()[:300]}")
        raise LaneGap(f"the next step for {self.name!r} was not recorded")


def read_through(registry: Any, quest_id: str) -> Tuple[Mapping[str, Any], str]:
    """``(tables, time zone)`` from the lane's ``read_spec_records`` tool. ``({}, "")`` when the lane has no such tool (it keeps no specifications: not a failure).
    A read that fails is a ``LaneGap`` with the tool's own words."""
    if registry is None or registry.get("read_spec_records") is None:
        return {}, ""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    try:
        result = registry.invoke("read_spec_records", {"quest_id": quest_id}, ToolContext(quest_id=quest_id))
    except Exception as error:      # noqa: BLE001
        raise LaneGap(f"the quest's specification records could not be read ({type(error).__name__}: {error})") from error
    if not result.ok:
        raise LaneGap("the quest's specification records could not be read: " + str(result.text).strip()[:300])
    data = getattr(result, "data", None) or {}
    return data.get("records") or {}, str(data.get("time_zone") or "")


def goals_through(registry: Any, quest_id: str) -> List[Mapping[str, Any]]:
    """The quest's goals, open and finished, from the lane's ``list_quest_goals`` tool. A lane without it, or a read that fails, is a ``LaneGap``: the reply must not claim a
    step is new when the goals could not be read."""
    if registry is None or registry.get("list_quest_goals") is None:
        raise LaneGap("this lane has no list_quest_goals tool, so the goals already on the milestone could not be listed")
    from quest_ai_runner.the_world.tools.tools import ToolContext
    try:
        result = registry.invoke("list_quest_goals", {"quest_id": quest_id, "open_only": False, "limit": 200}, ToolContext(quest_id=quest_id))
    except Exception as error:      # noqa: BLE001
        raise LaneGap(f"the quest's goals could not be listed ({type(error).__name__}: {error})") from error
    if not result.ok:
        raise LaneGap("the quest's goals could not be listed: " + str(result.text).strip()[:300])
    return [g for g in (result.data or []) if isinstance(g, Mapping)]


def resolve(registry: Any, quest_id: str, cue: str, now: Optional[datetime.datetime] = None) -> Optional[Resolved]:
    """The milestone a turn pinned to ``quest_id`` concerns, with its facts; None when the lane keeps no specifications, nothing was asked, or no milestone fits.
    Raises ``LaneGap`` when the records or goals cannot be read."""
    if registry is None or not quest_id or not str(cue or "").strip():
        return None
    tables, zone = read_through(registry, quest_id)
    if not tables:
        return None
    picked = select(tables, cue)
    if picked is None:
        return None
    rid, row, _ = picked
    week = Week.of(today_in(zone, now), zone)
    goals = goals_through(registry, quest_id)
    linked = metrics_of(tables, rid)
    metric = linked[0][1] if linked else None
    return Resolved(
        rid=rid, name=str(row.get("name") or rid), row=row, metric_ids=[mid for mid, _ in linked], owners=owners_of(row), week=week,
        facts=fact_block(tables, cue, week.monday, goals, zone), quest_id=quest_id,
        done_when=str(row.get("milestones") or "").strip()[:VALUE_CHARS].rstrip("."),
        value=first_sentence(str(metric.get("record") or "")) if metric else "")
