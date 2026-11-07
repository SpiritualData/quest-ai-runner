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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

STOP = frozenset("""a about all also an and any are as at be been but by can could do does for from get go going had has have how i in into is it its let lets
make me more my need new next not now of on one or our out please so some than that the their them then there these they this to up us want was we were what
when which who will with would you your""".split())
STEM = 6
NAME_WEIGHT, METRIC_WEIGHT, TEXT_WEIGHT = 3.0, 2.0, 1.0
MIN_SCORE = 2.0
CLOSE = 0.85           # a runner-up at this share of the best score counts as a tie
VALUE_CHARS = 220
PURPOSE_CHARS = 1200
ACT = "act"
CODE_ROUTED_ACTIONS = ("answer", "deep", "confirm", "clarify", "read")    # a plan that, for an act cue, the flow step runs before
BEYOND_THE_WRITE_TARGET = "code_or_files"                                 # work a tool write cannot do: the one case a deep run may follow the receipt
DELIVERABLE_TARGET = "draft_in_chat"                              # the planner reports a deliverable written in the reply: the first rung of the increment order


class LaneGap(RuntimeError):
    """The lane could not read or write what the milestone flow needs. It is raised, never swallowed: the reply is told what is missing."""


def words(text: Any) -> List[str]:
    out = []
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        token = token[:-1] if len(token) > 3 and token.endswith("s") else token
        if token not in STOP and len(token) > 2:
            out.append(token[:STEM] if len(token) > STEM else token)      # one stem for a word family: reliable and reliability, integration and integrations
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


CANDIDATES = 3
NONE = "NONE"


def candidates(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, limit: int = CANDIDATES) -> List[str]:
    """The ids of the milestones the cue might concern, best first: the few that clear the rarity-weighted overlap gate (model-free). Empty when none does."""
    index = index_of(tables)
    entries = list(index.get("entries") or [])
    docs = [{"name": set(words(e.get("title"))), "metric": set(words(e.get("metric_title"))), "text": set(words(e.get("summary")))} for e in entries]
    wanted = set(words(cue))
    containing = {w: sum(1 for d in docs if any(w in part for part in d.values())) for w in wanted}
    scored = []
    for entry, doc in zip(entries, docs):
        score = sum(math.log(1 + len(docs) / containing[w]) * max(NAME_WEIGHT * (w in doc["name"]), METRIC_WEIGHT * (w in doc["metric"]), TEXT_WEIGHT * (w in doc["text"]))
                    for w in wanted if containing.get(w))
        scored.append((score, entry["id"]))
    scored.sort(key=lambda pair: -pair[0])
    if not scored or scored[0][0] < MIN_SCORE:
        return []
    return [rid for score, rid in scored if score >= MIN_SCORE][:limit]


def judge(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, ids: Sequence[str], decide: Callable[[str], str]) -> Optional[str]:
    """The one cheap model judgment: which of the candidate milestones, if any, is the message asking to move forward. NONE is a first-class answer (the message is about
    something else: a reminder, a workflow, a recurring meeting, writing, a question). The reply is matched to the ids offered; anything that names none of them is NONE."""
    solutions = dict(solutions_of(tables))
    listing = "\n".join(f"{rid}: {solutions[rid].get('name')} | done when: {str(solutions[rid].get('milestones') or '').strip()[:VALUE_CHARS]}" for rid in ids if rid in solutions)
    answer = decide("A person wrote this message in their quest's chat:\n\n" + str(cue).strip()[:1500]
                    + "\n\nThese are milestones of the quest:\n" + listing
                    + "\n\nIs the message asking to move one of these milestones forward (make progress on it, get it going)? Reply with that milestone's id exactly as listed. "
                    f"Reply {NONE} if the message is about something else (a reminder, a workflow, a recurring meeting, writing content, preparing for a call, a question, "
                    "a status check). Reply with nothing else.")
    return next((rid for rid in ids if rid in str(answer)), None)


def applied_write_ran(gathered: Iterable[Mapping[str, Any]], registry: Any) -> bool:
    """True when a tool that changes something ended applied or already-there this turn and the write is one that stands in for the step: not a goal the planner wrote for something
    else (a deliverable goal under its own title is not the milestone's keyed next step). A write that was refused, blocked or held changed nothing."""
    from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_keyed_next_step
    if registry is None:
        return False
    for item in gathered:
        locator = str(item.get("locator") or "") if isinstance(item, Mapping) else ""
        spec = registry.get(locator[5:]) if locator.startswith("tool:") else None
        if spec is None or not getattr(spec, "mutates", False) or item.get("tool_status") not in ("applied", "noop", None):
            continue
        if locator == "tool:create_or_update_quest_goal" and not item.get("by_code"):
            title = ((item.get("call") or {}).get("args") or {}).get("title")
            if title and not is_keyed_next_step(title):
                continue
        return True
    return False


def parse_json(answer: str) -> Dict[str, Any]:
    """The JSON object in a model's reply ({} when there is none): a structured answer is read as data, never searched for words."""
    import json
    text = str(answer or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def supported_draft(draft: str, allowed: str) -> str:
    """The draft without any line that states a figure the records do not hold (a count of its own characters, an invented rate): a figure outside the milestone's record text is
    unsupported and its line is dropped, so a draft never carries a number that came from nowhere. List numbering and a [bracket] are not figures."""
    from quest_ai_runner.the_intelligence.mind.response_writer.interim_policy import FIGURE
    held = set(FIGURE.findall(allowed or ""))          # whole figures only: 12 is not held because 2026-12-31 contains it
    kept = []
    for line in str(draft or "").splitlines():
        body = re.sub(r"^\s*(?:[-*\u2022]\s*)?\d+[.)]\s+", "", line)
        body = re.sub(r"\[[^\]]*\]", "", body)
        if any(f not in held for f in FIGURE.findall(body)):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def draft_of(goal_text: str) -> str:
    """The draft a keyed goal already holds in its description (after its ``Draft:`` line), else "": a repeat reuses it, so it never rewrites the goal."""
    head, sep, rest = str(goal_text or "").partition("Draft:")
    return rest.strip() if sep else ""


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


def next_step_title(milestone_title: str, week: Week) -> str:
    """The title a person reads: the milestone record's own title (which starts with its code), then the week. The record id is the link, never part of the title."""
    return f"{milestone_title} next step: week of {week.monday}"


def is_open(goal: Mapping[str, Any]) -> bool:
    return not goal.get("completed") and not goal.get("abandoned")


def candidate_goals(goals: Sequence[Mapping[str, Any]], rid: str, name: str) -> List[Mapping[str, Any]]:
    """The open goals that plausibly belong to the milestone: linked to its solution record, titled with its record id (older keys), or sharing words with the milestone's own name
    (two or more, or its first word, which is its code when it has one). Candidates only: the model decides which of them is the same milestone."""
    name_words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP]
    first = name_words[0] if name_words else ""
    out = []
    for goal in goals:
        if not is_open(goal):
            continue
        title_words = set(re.findall(r"[a-z0-9]+", str(goal.get("title") or "").lower()))
        shared = {w for w in name_words if w in title_words}
        if (str(goal.get("spec_solution_id") or "") == rid or str(goal.get("title") or "").startswith(rid + " ")
                or len(shared) >= 2 or (first and first in title_words)):
            out.append(goal)
    return sorted(out, key=lambda g: str(g.get("spec_solution_id") or "") != rid)      # linked by the record first


def linked_goals(goals: Sequence[Mapping[str, Any]], rid: str) -> List[Mapping[str, Any]]:
    """The goals already on the quest for this milestone by id: linked to its solution record, or titled with its record id (the key every goal of it carries)."""
    return [g for g in goals if str(g.get("spec_solution_id") or "") == rid or str(g.get("title") or "").startswith(rid + " ")]


def goal_summary(goal: Mapping[str, Any]) -> str:
    state = "done" if goal.get("completed") else ("abandoned" if goal.get("abandoned") else "open")
    return f"{goal.get('title')!r} [{state}, due {goal.get('due') or 'no date'}, {goal.get('assignee_name') or goal.get('assignee_user_id') or 'unassigned'}]"


INCREMENT_ORDER = ("Increment order, the first that applies (one increment per turn): "
                   "1. a deliverable the milestone needs that you can write from what you have: write it in the reply (deep_target draft_in_chat) and record it as a goal keyed "
                   "{title} followed by a short name; "
                   "2. else a metric value you can read from the data your tools reach: record it with the metric's record id and its source and date; "
                   "3. else an owned next step that names what only a person can do. Code records that step for you under its key and returns the receipt, "
                   "so do not choose a deep run, do not call create_or_update_quest_goal for it, and never ask permission to do it.")


def fact_block(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, monday: str = "", goals: Sequence[Mapping[str, Any]] = (),
               zone: str = "", rid: str = "") -> str:
    """The facts for the planner, or "" when no milestone fits the cue. ``rid`` names the milestone when it was already judged to be the one."""
    picked = (rid, dict(solutions_of(tables)).get(rid) or {}, []) if rid else select(tables, cue)
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
    lines.append(f"- Purpose as recorded (it names the owners): {str(row.get('purpose') or '').strip()[:PURPOSE_CHARS] or 'none recorded'} | tracking_owner: {', '.join(owners) or 'empty'}")
    lines.append("- Assign from the owners named in that purpose text or tracking_owner; any listed owner is acceptable, so never ask who should own it. Leave it unassigned only when no owner is named anywhere.")
    if ties:
        lines.append("- Close runners-up (the quest lists this one first): " + ", ".join(f"{t} {str((tables['solutions'].get(t) or {}).get('name') or '')[:50]!r}" for t in ties))
    already = candidate_goals(goals, rid, name)
    lines.append("- Open goals on the quest that may be this milestone's (linked by spec_solution_id, or sharing its name or code, whatever the title or week; the code decides which is the same milestone when it records the step): "
                 + ("; ".join(goal_summary(g) for g in already) if already else "none")
                 + (". If the step exists here, it is updated in place, not created again: say 'already there' or 'updated', never that nothing was recorded." if already else "."))
    lines.append(f"- Keys: this week's next step is the goal titled '{next_step_title(name, Week(monday or '<Monday date>', '', ''))}'"
                 + (f" (week read in {zone})" if zone else " (the quest records no time zone, so UTC)") + f", linked to spec_solution_id {rid}"
                 + (f" and spec_metric_id {metric_ids[0]}" if metric_ids else "") + f"; a deliverable is a goal titled '{name} <short name>'. The record id is only the link (spec_solution_id). The tool reports created, updated or already there.")
    lines.append(INCREMENT_ORDER.replace("{title}", name))
    lines.append("This cue asks to move the milestone forward by recording its next step. It never means rescheduling the milestone's own date, and it is not a question to the person.")
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
    purpose: str = ""
    candidates: List[Mapping[str, Any]] = field(default_factory=list)
    receipt: str = field(default="")
    draft: str = ""
    after_state: Optional[Mapping[str, Any]] = None      # the goal as the tool read it back after the write (id, name, assignee id, links, period)

    def routes_to_code(self, plan: Any, user_intent: Optional[str], gathered: Sequence[Mapping[str, Any]], registry: Any) -> bool:
        """True when the person asked for something to be done (the planner's ``user_intent``), no tool has already changed something this turn (a refused or blocked write has not),
        and the plan is one the flow step takes over. A deliverable the planner means to write in the reply does not exempt it: the step records the owned next step with its own draft, so a
        reply that fails to record anything still ends with the step kept. Structured fields only."""
        return (not self.receipt and user_intent == ACT and getattr(plan, "action", None) in CODE_ROUTED_ACTIONS
                and not applied_write_ran(gathered, registry))

    def doing(self) -> str:
        """What happens now, for the first message: stated from the records, before the write."""
        week = next_step_title(self.name, self.week)
        exists = any(str(g.get("title") or "").strip() == week for g in self.candidates)
        return (f"Updating this week's next step for {self.name}, which is already on the quest, and keeping its owner and draft." if exists
                else f"Recording this week's next step for {self.name}, linked to the milestone, with its owner.")

    def settle_plan(self, plan: Any, receipt: str) -> None:
        """Make the plan agree with what code already did. A read plan is left alone. A deep run stays only when the work needs files or code beyond the tool write, and then its brief
        starts with what the write already did; every other plan (a deep run, a question, a request for permission) becomes the answer, which reports the receipt."""
        if getattr(plan, "action", None) == "read":
            return
        if getattr(plan, "action", None) == "deep" and (getattr(plan, "deep_target", None) == BEYOND_THE_WRITE_TARGET or getattr(plan, "deep_subtasks", None)):
            plan.deep_brief = f"Already done by the runner before this run (do not redo it): {receipt}\n\n{plan.deep_brief or plan.goal or ''}"
            return
        plan.action = "answer"
        plan.deferred_deep = None
        plan.confirm_question = None
        plan.asks_first = False

    def allowed_text(self) -> str:
        """Everything a draft may take its figures from: the milestone's record text and the request. A figure outside it is not in the records."""
        return " ".join([self.name, self.done_when, self.value, self.purpose, self.facts])

    def related_text(self) -> str:
        """The person's own goals for this milestone are linked to only by being named here, in this goal's description; they are never changed."""
        from quest_ai_runner.the_body.hands.quest_operations.goal_links import adoptable
        titles = [str(g.get("title")) for g in self.candidates if g.get("title") and not adoptable(g.get("title"), next_step_title(self.name, self.week))][:4]
        return (" Related goals on the quest: " + "; ".join(titles) + ".") if titles else ""

    def goal_arguments(self) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "quest_id": self.quest_id, "title": next_step_title(self.name, self.week), "period": self.week.period, "due_date": self.week.due,
            "description": (f"Next step for {self.name}. Done when: {self.done_when}. Current value: {self.value or 'not recorded yet'}."
                            + self.related_text()),
            "spec_solution_id": self.rid, "match_by_link": True, "user_asked_for_this_field": True}
        if self.metric_ids:
            args["spec_metric_id"] = self.metric_ids[0]
        return args

    LEADS = {"created": "I created this week's next step for {name}.",
             "updated": "I updated the existing next step for {name} in place (no second goal was made).",
             "unchanged": "This week's next step for {name} was already set, so nothing was changed."}

    def lead_sentence(self, state: str) -> str:
        """The plain statement of what the write did, composed by code from the tool's typed state: created, updated in place, or already set."""
        return self.LEADS.get(state, "I recorded this week's next step for {name}.").format(name=self.name)

    def choices(self, decide: Optional[Callable[[str], str]]) -> Dict[str, str]:
        """The three things that need reading the record text, in ONE cheap call: ``owner`` (when the record's ``tracking_owner`` names nobody, the person the purpose text names as
        doing the work), ``goal_id`` (the keyed next-step goal on the quest that is already this milestone's next step, any week, any older title; only keyed goals are offered,
        a person's goal never is) and ``draft`` (the cheap deliverable for the step, kept in the goal; not asked again when the existing keyed goal already holds one)."""
        from quest_ai_runner.the_body.hands.quest_operations.goal_links import adoptable
        keyed = [g for g in self.candidates if adoptable(g.get("title"), next_step_title(self.name, self.week))]       # the step's own earlier goals only; a person's goal is never offered
        if decide is None:
            return {"owners": [], "goal_id": "", "draft": ""}
        this_week = next((g for g in keyed if str(g.get("title") or "").strip() == next_step_title(self.name, self.week)), None)
        kept_draft = draft_of(str((this_week or {}).get("text") or ""))
        listing = "\n".join(f"{g.get('id')}: {g.get('title')} | {str(g.get('text') or '')[:200]}" for g in keyed) or "(none)"
        asks = []
        if not self.owners and self.purpose.strip():
            asks.append('"owners": every person named as an owner in the milestone text above, as written (names only, no team names), the one who does the hands-on work first, or [] when it names none')
        if keyed:
            asks.append('"goal_id": the id, exactly as listed, of the open goal below that is already the next step of this milestone (the same work, whatever its title or week), or ""')
        if not kept_draft:
            asks.append('"draft": the deliverable itself, never a plan to write it, short enough to send or use as it is, under 120 words, in the form the milestone text calls for: copy that is '
                        'to be compared comes as distinct alternatives with the metric they are compared by; questions to put to people are few, neutral, each a full sentence ready to send '
                        'as written; something addressed to an outside party is a short message stating what they get and the ask; a new source or channel is one named concrete '
                        'source with a one-line hypothesis, a first test and the milestone metric; a checklist of at most 5 steps only when none of those fits. Use only the facts above; '
                        'a [bracket] only for a fact that is truly unknown (a link); no prices, dates, counts, rates or claims that are not given above, and never a count of its own '
                        'characters, words or items; no em dashes')
        if not asks:
            return {"owners": [], "goal_id": "", "draft": kept_draft}
        answer = decide(f"Milestone: {self.name}. Done when: {self.done_when}. Current value: {self.value or 'not recorded yet'}.\n\nThe milestone text:\n{self.purpose[:PURPOSE_CHARS] or '(none)'}\n\n"
                        f"Open goals on the same quest:\n{listing}\n\nReturn JSON with these keys only: " + "; ".join(asks) + ". Reply with the JSON only.")
        data = parse_json(answer)
        goal = str(data.get("goal_id") or "")
        named = data.get("owners") if isinstance(data.get("owners"), list) else [data.get("owner")]
        return {"owners": [str(n).strip(" .'\"") for n in named if n and str(n).strip(" .'\"")], "goal_id": goal if goal in {str(g.get("id")) for g in keyed} else "",
                "draft": kept_draft or supported_draft(str(data.get("draft") or "").strip(), self.allowed_text())}

    def carry_out(self, registry: Any, decide: Optional[Callable[[str], str]] = None) -> Tuple[str, str]:
        """The keyed idempotent write, then its receipt: ``(receipt text, the tool's typed status)``. A goal already doing this milestone's next step under an older title is
        updated in place (renamed to the key, linked, moved to the week), never duplicated; a repeat finds the keyed goal and says it is already there. Each owner is tried as
        assignee in turn; with none that Quest accepts the step is written unassigned and the receipt says so. A tool the lane lacks, or a write that failed or is held, is a
        ``LaneGap`` carrying the tool's own words. ``decide(prompt) -> text`` is one cheap model call used for the two choices that need reading the record text."""
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if registry is None or registry.get("create_or_update_quest_goal") is None:
            raise LaneGap("this lane has no create_or_update_quest_goal tool, so the next step could not be recorded")
        ctx = ToolContext(quest_id=self.quest_id)
        base = self.goal_arguments()
        try:
            picked = self.choices(decide)
        except Exception as error:      # noqa: BLE001  the choice failed: surfaced, not guessed
            raise LaneGap(f"choosing the owner or the existing goal failed ({type(error).__name__}: {error})") from error
        owners = list(self.owners) or list(picked["owners"])
        if picked["goal_id"]:
            base["adopt_goal_id"] = picked["goal_id"]
        if picked["draft"]:
            base["description"] = base["description"] + f"\nDraft: {picked['draft']}"
            self.draft = picked["draft"]
        refused: List[str] = []
        for owner in [*owners, ""]:
            args = dict(base, assignee=owner) if owner else dict(base, clear_assignee=True) if refused else dict(base)
            try:
                result = registry.invoke("create_or_update_quest_goal", args, ctx)
            except Exception as error:      # noqa: BLE001  surfaced as a gap, with the cause
                raise LaneGap(f"recording the next step failed ({type(error).__name__}: {error})") from error
            if result.ok:
                self.receipt = result.text
                self.after_state = (result.data or {}).get("goal")
                named = ", ".join(owners)
                note = (f" The owner could not be set ({'; '.join(refused)}), so the goal is left unassigned. Say who should own it ({named}, as the record names them) and ask nothing."
                        if refused and not owner else "")
                lead = self.lead_sentence(str((result.data or {}).get("state") or ""))
                if (result.data or {}).get("problems"):
                    note += " The quest does not show everything that was asked for (" + "; ".join(result.data["problems"]) + "): say exactly that, and do not say it is linked or owned."
                if self.draft:
                    note += f" The draft kept in the goal, to show in the reply: {self.draft}"
                return (f"RECEIPT written by code for {self.name!r}. Outcome: {lead} {result.text}{note} "
                        f"Open the reply with exactly this sentence: \"{lead}\" Then say who owns it and what only a person can do, "
                        "and do not offer to do it or ask whether to. This request is carried out: ask the person nothing, propose no new date, and do not move the milestone's own date. "
                        "Show the kept draft exactly as given and state no count of its items. State no figure that is not in the records."), result.status.value
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


def resolve(registry: Any, quest_id: str, cue: str, now: Optional[datetime.datetime] = None, decide: Optional[Callable[[str], str]] = None) -> Optional[Resolved]:
    """The milestone a turn pinned to ``quest_id`` concerns, with its facts; None when the lane keeps no specifications, nothing was asked, or no milestone fits.
    Raises ``LaneGap`` when the records or goals cannot be read."""
    if registry is None or not quest_id or not str(cue or "").strip():
        return None
    tables, zone = read_through(registry, quest_id)
    if not tables:
        return None
    ids = candidates(tables, cue)
    if not ids:
        return None
    rid = ids[0]
    if decide is not None:
        # The candidates are model-free; one cheap judgment says whether the message is asking to move one of them, and NONE is an answer.
        try:
            rid = judge(tables, cue, ids, decide)
        except Exception as error:      # noqa: BLE001  a judgment that failed is a gap, not a guess either way
            raise LaneGap(f"deciding which milestone the message concerns failed ({type(error).__name__}: {error})") from error
        if rid is None:
            return None
    row = dict(solutions_of(tables))[rid]
    week = Week.of(today_in(zone, now), zone)
    goals = goals_through(registry, quest_id)
    linked = metrics_of(tables, rid)
    metric = linked[0][1] if linked else None
    return Resolved(
        rid=rid, name=str(row.get("name") or rid), row=row, metric_ids=[mid for mid, _ in linked], owners=owners_of(row), week=week,
        facts=fact_block(tables, cue, week.monday, goals, zone, rid), quest_id=quest_id,
        done_when=str(row.get("milestones") or "").strip()[:VALUE_CHARS].rstrip("."),
        value=first_sentence(str(metric.get("record") or "")) if metric else "",
        purpose=str(row.get("purpose") or ""), candidates=candidate_goals(goals, rid, str(row.get("name") or rid)))
