"""Which milestone of a quest a short cue concerns, found by code before the planner plans.

Role: an arm of the Spec Selection idea (choose, from an ESOI specification, the few records a request concerns) for the records a quest keeps
in Quest. A cue such as "get the release moving" names no record; the quest's solutions (the strategies, each with its milestones and metric) are
scored against it by the words they share, weighted by how rare each word is among them, one cheap judgment says whether the message asks for
progress on one of them (NONE for a reminder, bookkeeping alone or a question), and the chosen one is handed to the planner as a block of facts: the
solution id, what done means, the metric and its current value, the owners the record lists, the goals already linked to it, and the increment order.
Nothing here reads the model's output, and resolving a milestone only reads; the work a progress turn does is ``milestone_progress``. A failure to read
is a ``LaneGap``, never an empty result. When two solutions are close the one the quest lists first wins: a quest lists its solutions in the order its
owners ranked them.
"""
from __future__ import annotations

import datetime
import logging
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
FULL_VALUE_CHARS = 1500       # a metric record's whole text goes to a draft's prompt up to this size, clipped at a boundary
PURPOSE_CHARS = 1200
ACT = "act"
PROGRESS_INTENTS = (ACT, "ask")       # the planner's reading of a message the milestone judgment found asks for progress
OWNED_WRITES = ("create_or_update_quest_goal", "create_quest_goal")       # goal writes of a milestone cue belong to the code step, which keeps the deliverable inside one keyed goal
CODE_ROUTED_ACTIONS = ("answer", "deep", "confirm", "clarify", "read")    # a plan that, for an act cue, the flow step runs before
BEYOND_THE_WRITE_TARGET = "code_or_files"                                 # work a tool write cannot do: the one case a deep run may follow the receipt
DELIVERABLE_TARGET = "draft_in_chat"                              # the planner reports a deliverable written in the reply: the first rung of the increment order


log = logging.getLogger("quest-ai-runner.milestone-cue")


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


JUDGE_ASKS = 2      # an answer that names no offered option and is not NONE is no verdict: the question is asked once more


def judged_option(answer: Any, ids: Sequence[str], names: Optional[Mapping[str, str]] = None) -> Tuple[bool, Optional[str]]:
    """``(is a verdict, the chosen id)`` for one judgment answer: the id offered, the id without its prefix (``esr_``), or, when the whole answer is one offered
    milestone's name or the label its name starts with ("M2" for "M2 Launch the beta") and no other option shares it, that milestone. NONE is a verdict
    with no id. Anything else (an empty answer, an id that was not offered) is no verdict."""
    text = str(answer or "").strip()
    for rid in ids:
        bare = rid.split("_", 1)[-1]
        if rid in text or (len(bare) >= 6 and bare in text):
            return True, rid
    said = re.sub(r"[^\w ]+", "", text).strip().lower()
    if said and names:
        labels = {rid: {str(names.get(rid) or "").strip().lower(), (str(names.get(rid) or "").split() or [""])[0].lower()} - {""} for rid in ids}
        matched = [rid for rid in ids if said in labels[rid]]
        if len(matched) == 1:
            return True, matched[0]
    if re.search(rf"\b{NONE}\b", text, re.IGNORECASE):
        return True, None
    return False, None


def judge(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], cue: str, ids: Sequence[str], decide: Callable[[str], str]) -> Optional[str]:
    """The one cheap model judgment: which of the candidate milestones, if any, is the message asking to move forward. NONE is a first-class answer (the message is about
    something else: a reminder, a workflow, a recurring meeting, writing, a question). The reply is matched to the options offered (``judged_option``); an answer that names
    no option and is not NONE is no verdict and is asked once more (``JUDGE_ASKS``), then read as NONE. (Measured offline on the chat cases, 2026-11-08, on the production path: the
    answer-at-once listing said NONE to plain requests for progress on 15 of 20 tries; with the purpose shown and one line before the verdict it chose right; one YES/NO question
    per candidate and a two-view Decision Check both chose worse.)"""
    solutions = dict(solutions_of(tables))
    offered = [rid for rid in ids if rid in solutions]
    # Each option carries its purpose too: milestones of one area (several sources of users, say) differ there, not in their names or done-when lines.
    listing = "\n".join(f"{rid}: {solutions[rid].get('name')} | done when: {clip(solutions[rid].get('milestones'), VALUE_CHARS)}"
                        + (f" | why: {clip(solutions[rid].get('purpose'), VALUE_CHARS)}" if solutions[rid].get("purpose") else "") for rid in offered)
    # One short line first: answering at once, a cheap model often said NONE to a plain request for progress; the verdict is read from the last line only.
    prompt = ("A person wrote this message in their quest's chat:\n\n" + str(cue).strip()[:1500]
              + "\n\nThese are milestones of the quest:\n" + listing
              + "\n\nIs the message asking for work that moves one of these milestones forward: making progress on it, getting it going, or bringing its numbers or records "
              "up to date? A request worded as a question is still a request, and the message need not use the milestone's words. "
              f"It is {NONE} only if what it asks for is something other than progress on a milestone: a reminder, a workflow, a recurring meeting, writing content, "
              "preparing for a call, bookkeeping alone (changing an owner, a due date or a reminder), or information alone (how something stands, with nothing asked to be done). "
              f"First write one short line saying what the message asks for, without any id. Then, on the last line, write only that milestone's id exactly as listed, or {NONE}.")
    names = {rid: str(solutions[rid].get("name") or "") for rid in offered}
    for attempt in range(JUDGE_ASKS):
        answer = decide(prompt)
        lines = [line for line in str(answer or "").splitlines() if line.strip()]
        settled, chosen = judged_option(lines[-1] if lines else "", offered, names)
        if settled:
            return chosen
        log.info("milestone cue: the judgment answered %r, which names no option (ask %d of %d)", str(answer)[:300], attempt + 1, JUDGE_ASKS)
    return None


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


def clip(text: Any, limit: int) -> str:
    """``text`` shortened to at most ``limit`` characters without ever cutting inside a word or an identifier: it ends at a sentence end, else at a clause break (a comma, semicolon or
    opening bracket) in the second half, else at the last whole word. A text that fits is returned whole (whitespace collapsed)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace():
        head = head.rsplit(" ", 1)[0] if " " in head else head      # the limit fell inside a token: the token is left out, never cut
    sentence = [m.start() for m in re.finditer(r"[.;:](?=\s)", head)]
    if sentence:
        return head[:sentence[-1]].rstrip()
    clause = [m.start() for m in re.finditer(r",\s|\s\(", head) if m.start() > limit // 2]
    return (head[:clause[-1]] if clause else head).rstrip(" ,;:(")


def first_sentence(text: str, limit: int = 160) -> str:
    """The start of a recorded value: its first sentence when that fits the limit, else the text clipped at a clause break (``clip``), never inside a word or an identifier."""
    text = " ".join(str(text or "").split())
    cut = re.search(r"[.;](\s|$)", text[:limit + 1])
    return (text[:cut.start()] if cut else clip(text, limit)).strip()


def owners_of(row: Mapping[str, Any]) -> List[str]:
    """The Quest user ids of the people the milestone's record lists as responsible, from its structured ``owner_user_ids`` column. A user id is accepted as the
    assignee of a goal as it stands, so no name is matched here."""
    raw = row.get("owner_user_ids") or []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[;,]", str(raw))
    return [str(part).strip() for part in items if str(part).strip()]


CURRENT_VALUE = "Current value:"
VALUE_LINE = re.compile(r"(?m)^Current value:.*$")
READ_TOOL = "read_metric_source"
RECORDED_TOOL = "read_recorded_metric_value"       # the host own store of recorded metric values, asked before the record named source


def current_value_line(value: Any, as_of: str, source: str) -> str:
    """The one line a keyed goal holds for a measured value: ``Current value: <value> (as of <YYYY-MM-DD>, from <source label>)``."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import shown
    return f"{CURRENT_VALUE} {shown(value)} (as of {as_of}, from {source})"


def value_line_of(text: str) -> str:
    """The ``Current value:`` line a goal's text already holds, else ""."""
    found = VALUE_LINE.search(str(text or ""))
    return found.group(0).strip() if found else ""


def source_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], rid: str, row: Mapping[str, Any]) -> str:
    """The name of the source the milestone's value is read from: its metric record's ``source`` column, else the solution record's own."""
    for _, metric in metrics_of(tables, rid):
        if str(metric.get("source") or "").strip():
            return str(metric["source"]).strip()
    return str(row.get("source") or "").strip()


def measured_through_of(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], rid: str, row: Mapping[str, Any]) -> str:
    """The day the milestone's recorded value covers (``measured_through``), from its metric record, else the solution record; "" when none."""
    for _, metric in metrics_of(tables, rid):
        if str(metric.get("measured_through") or "").strip():
            return str(metric["measured_through"]).strip()
    return str(row.get("measured_through") or "").strip()


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


INCREMENT_ORDER = ("This turn's increment is made by code, the first of these that applies: 1. the deliverable the milestone's done test needs that an AI can write now; "
                   "2. a fresh reading of its metric; 3. evidence for the next decision; 4. a better recommendation. Code measures it before and after with a check that is "
                   "not the writer, keeps it on the goal titled '{title} deliverables' (updated in place on a repeat), reads the milestone's metric again and names at most one "
                   "step only a person can do. So do not choose a deep run, do not write a goal for it, and never ask permission. An owner, a due date or a reminder is "
                   "never the progress.")


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
             f"- Milestone: {name} | Done when: {clip(row.get('milestones'), VALUE_CHARS).rstrip('.')} | Current value: {value or 'not recorded yet'}."]
    if metric:
        lines.append(f"- Metric record {metric_ids[0]} ({metric.get('name')}); its full recorded text is in read_spec_records.")
    lines.append(f"- Purpose as recorded (it names the owners): {clip(row.get('purpose'), PURPOSE_CHARS) or 'none recorded'} | owner_user_ids: {', '.join(owners) or 'empty'}")
    lines.append("- Code assigns the step to a user in owner_user_ids; any listed owner is acceptable, so never ask who should own it. With none listed, the owner is the person the purpose text names; leave it unassigned only when no owner is named anywhere.")
    source = source_of(tables, rid, row)
    if source:
        through = measured_through_of(tables, rid, row)
        lines.append(f"- Metric source: {source}. Code reads it (read_metric_source) and records the line 'Current value: <value> (as of <date>, from <source>)' in the keyed next step, replacing the line in place on a repeat; "
                     "do not edit the metric record for the value, and state the value, its date and its source in the reply, exactly as the receipt gives them."
                     + (f" The metric record's own value covers through {through}." if through else ""))
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
    lines.append("This cue asks to move the milestone forward with work the AI does itself. It never means rescheduling the milestone's own date, and it is not a question to the person.")
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
    source: str = ""                                     # the name of the metric source the record gives ("" when it names none)
    reading: Optional[Mapping[str, Any]] = None          # what read_metric_source returned: value, as_of, source (label), detail
    reading_gap: str = ""                                # why the source could not be read, in the tool's words
    kept_line: str = ""                                  # the Current value line the keyed goal already holds
    grounding: str = ""                                  # what the turn read and was shown: the only source for a draft's claim about a product or past work
    value_full: str = ""                                 # the metric record's whole text (whitespace collapsed): what a draft is written from, never a cut of it
    records_text: str = ""                               # the milestone's record and its metric records as the code read them
    record_date: str = ""                                # the day the metric record's own reading is from (its measured_through, else a day in its text)
    record_line: str = ""                                # the metric record's first line as written: a dated "Current value: ... (as of ..., from ...)" is a recorded reading
    request: str = ""                                    # what the person asked this turn, in their words: the step and its draft serve it, within the milestone
    tables: Mapping[str, Any] = field(default_factory=dict)        # the quest's specification records as read when the cue was resolved
    progress: Any = None                                 # what the progress step made and measured (``milestone_progress.Progress``), None until it ran
    lead: str = ""                                       # the code-composed sentence saying what the write did (created, updated in place, already set), whole

    def routes_to_code(self, plan: Any, user_intent: Optional[str], gathered: Sequence[Mapping[str, Any]], registry: Any) -> bool:
        """True when the person asked for progress (the milestone judgment and the planner's ``user_intent``), no tool has already changed something this turn (a refused or
        blocked write has not), and the plan is one the flow step takes over. A deliverable the planner means to write in the reply does not exempt it: the progress step makes and
        keeps the deliverable itself. Structured fields only."""
        # The milestone judgment already answered "is this asking for work that moves the milestone" (a question about how it stands is NONE there and resolves no
        # milestone), so a request worded as a question ("can we get it moving?") that the planner reads as ``ask`` is still progress; only a hold-off or a statement is not.
        return (not self.receipt and user_intent in PROGRESS_INTENTS and getattr(plan, "action", None) in CODE_ROUTED_ACTIONS
                and not applied_write_ran(gathered, registry))

    def doing(self) -> str:
        """What happens now, for the first message: stated from the records, before the work: the increment is made and measured, never a step recorded for someone."""
        return (f"Making the next piece of work for {self.name} that does not wait on anyone, measuring it before and after, and reading the milestone's metric again.")

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

    def current_text(self) -> str:
        """The milestone's current value for a prompt: the fresh reading of its source when the code step has one (with its day and source), else the metric record's first sentence."""
        if self.reading:
            return current_value_line(self.reading["value"], self.reading["as_of"], self.reading["source"]).removeprefix(CURRENT_VALUE).strip()
        return clip(self.value_full, FULL_VALUE_CHARS) or self.value or "not recorded yet"

    def allowed_text(self) -> str:
        """Everything a draft may take its figures from: the milestone's record text and the request. A figure outside it is not in the records. With a fresh reading the older figure of
        the metric record (and the fact block that quotes it) is history, not a source for the draft: a draft never states a figure that contradicts the reading."""
        if self.reading:
            return " ".join([self.name, self.done_when, self.purpose, self.current_text(), str(self.reading.get("detail") or "")])
        return " ".join([self.name, self.done_when, self.value, self.value_full, self.purpose, self.facts])

    def read_entries(self) -> List[Dict[str, Any]]:
        """What the code step read, shaped like the tool results the loop gathers (``by_code``), so the writer, the figure pass and the judge see the milestone's record, the metric
        record's text and the source's reading."""
        def entry(tool: str, text: str) -> Dict[str, Any]:
            return {"kind": "query", "locator": f"tool:{tool}", "text": text, "tool_status": "applied", "by_code": True}
        out = []
        if self.records_text:
            out.append(entry("read_spec_records", self.records_text))
        if self.reading:
            out.append(entry(READ_TOOL, current_value_line(self.reading["value"], self.reading["as_of"], self.reading["source"])
                             + (f"\nDetail: {self.reading['detail']}" if self.reading.get("detail") else "")))
        return out

    def read_source(self, registry: Any) -> None:
        """Read the milestone's metric source, when its record names one, through the lane's ``read_metric_source`` tool. A reading is kept for the goal's one
        ``Current value:`` line; a source that cannot be read leaves the line the goal already holds and is said in the receipt, never replaced by a guess."""
        goal = next((g for g in self.candidates if str(g.get("title") or "").strip() == next_step_title(self.name, self.week)), None) \
            or next((g for g in self.candidates if str(g.get("spec_solution_id") or "") == self.rid), None)
        existing = value_line_of(str((goal or {}).get("text") or (goal or {}).get("description") or ""))
        self.kept_line = existing if "(as of " in existing else ""
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if self.metric_ids and registry is not None and registry.get(RECORDED_TOOL) is not None:
            # The host's own store comes first: the latest value recorded for the metric record (in Quest, its collection's newest entry). No value recorded there is not a gap:
            # the record's named source is tried next.
            try:
                recorded = registry.invoke(RECORDED_TOOL, {"quest_id": self.quest_id, "metric_id": self.metric_ids[0]}, ToolContext(quest_id=self.quest_id))
            except Exception as error:      # noqa: BLE001  the next source is tried; the cause is kept if there is none
                recorded = None
                self.reading_gap = f"reading the value recorded for the metric failed ({type(error).__name__}: {error})"
            if recorded is not None and recorded.ok and isinstance(recorded.data, Mapping) and recorded.data.get("as_of"):
                stored = {k: recorded.data[k] for k in ("value", "as_of", "source", "detail") if k in recorded.data}
                # A stored value older than the day the metric record was last measured is not the current value: the record has moved on since, so it is not used.
                if not self.record_date or str(stored["as_of"]) >= self.record_date[:10]:
                    self.reading = stored
                    return
        # Next, the reading the metric record itself holds, dated and sourced on its first line. Any value is a reading, 0 included. The named source is still read after
        # it and replaces it when it reads as recently or later; a source that cannot be read leaves the recorded reading standing, and the receipt says so.
        self.reading = self.recorded_reading()
        if not self.source:
            return
        if registry is None or registry.get(READ_TOOL) is None:
            self.reading_gap = f"this lane has no {READ_TOOL} tool, so the source {self.source!r} could not be read"
            return
        try:
            result = registry.invoke(READ_TOOL, {"name": self.source}, ToolContext(quest_id=self.quest_id))
        except Exception as error:      # noqa: BLE001  surfaced in the receipt with its cause
            self.reading_gap = f"reading the source {self.source!r} failed ({type(error).__name__}: {error})"
            return
        if result.ok and isinstance(result.data, Mapping) and result.data.get("as_of"):
            if not self.reading or str(result.data["as_of"]) >= str(self.reading.get("as_of") or ""):
                self.reading = {k: result.data[k] for k in ("value", "as_of", "source", "detail") if k in result.data}
        else:
            self.reading_gap = str(result.text).strip()[:300]

    def recorded_reading(self) -> Optional[Dict[str, Any]]:
        """The reading the metric record's first line holds (``Current value: <value> (as of <YYYY-MM-DD>, from <source>)``), else None. A value of 0 is a reading."""
        found = self.KEPT_READING.match(self.record_line or "")
        if not found or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", found.group("day").strip()) or not found.group("value").strip():
            return None
        return {"value": found.group("value").strip(), "as_of": found.group("day").strip(), "source": found.group("source").strip(), "detail": {"from": "the metric record"}}


    KEPT_READING = re.compile(r"^Current value:\s*(?P<value>.*?)\s*\(as of (?P<day>[^,)]+), from (?P<source>.*)\)\s*$")


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


def records_read(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], rid: str, row: Mapping[str, Any]) -> str:
    """The milestone's record and its metric records as the code read them, in words a reader of the turn's reads can check a figure or a date against."""
    parts = [f"Milestone record: {row.get('name') or rid}. Done when: {str(row.get('milestones') or '').strip()}. Purpose: {str(row.get('purpose') or '').strip()}. "
             f"Outputs: {str(row.get('outputs') or '').strip()}. Measures of success: {str(row.get('measures_of_success') or '').strip()}."]
    for _, metric in metrics_of(tables, rid):
        parts.append(f"Metric record: {metric.get('name')}. Recorded: {str(metric.get('record') or '').strip()}. Source: {metric.get('source') or ''}. "
                     f"Measured through: {metric.get('measured_through') or ''}.")
    return "\n".join(parts)


def goals_through(registry: Any, quest_id: str, rid: Optional[str] = None) -> List[Mapping[str, Any]]:
    """With ``rid``, the milestone's own open goals: the lane's ``list_quest_goals`` asked for the goals linked to the milestone's record
    (``spec_solution_id``, the link the flow writes on every step it records), open only, which the server filters. Reading the quest's goals instead
    showed one page of 100, soonest due first, so on a quest with hundreds of goals this week's step was never seen. Without ``rid``, the quest's goals
    as the tool lists them (one page). A lane without the tool, or a read that fails, is a ``LaneGap``: the reply must not claim a step is new when the
    goals could not be read."""
    if registry is None or registry.get("list_quest_goals") is None:
        raise LaneGap("this lane has no list_quest_goals tool, so the goals already on the milestone could not be listed")
    from quest_ai_runner.the_world.tools.tools import ToolContext
    try:
        args = {"quest_id": quest_id, "spec_solution_id": rid, "open_only": True, "limit": 100} if rid else {"quest_id": quest_id, "open_only": False, "limit": 200}
        result = registry.invoke("list_quest_goals", args, ToolContext(quest_id=quest_id))
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
        log.info("milestone cue: no milestone clears the overlap gate")
        return None
    rid = ids[0]
    if decide is not None:
        # The candidates are model-free; one cheap judgment says whether the message is asking to move one of them, and NONE is an answer.
        try:
            rid = judge(tables, cue, ids, decide)
        except Exception as error:      # noqa: BLE001  a judgment that failed is a gap, not a guess either way
            raise LaneGap(f"deciding which milestone the message concerns failed ({type(error).__name__}: {error})") from error
        if rid is None:
            log.info("milestone cue: the judgment matched none of %s", ids)
            return None
    row = dict(solutions_of(tables))[rid]
    week = Week.of(today_in(zone, now), zone)
    goals = goals_through(registry, quest_id, rid)
    linked = metrics_of(tables, rid)
    metric = linked[0][1] if linked else None
    return Resolved(
        rid=rid, name=str(row.get("name") or rid), row=row, metric_ids=[mid for mid, _ in linked], owners=owners_of(row), week=week, tables=tables,
        facts=fact_block(tables, cue, week.monday, goals, zone, rid), quest_id=quest_id, request=str(cue or "").strip(),
        done_when=clip(row.get("milestones"), VALUE_CHARS).rstrip("."),
        value=first_sentence(str(metric.get("record") or "")) if metric else "",
        value_full=" ".join(str(metric.get("record") or "").split()) if metric else "",
        purpose=str(row.get("purpose") or ""), candidates=candidate_goals(goals, rid, str(row.get("name") or rid)), source=source_of(tables, rid, row), records_text=records_read(tables, rid, row),
        record_date=measured_through_of(tables, rid, row) or next(iter(re.findall(r"\d{4}-\d{2}-\d{2}", str(metric.get("record") or ""))), "") if metric else "",
        record_line=str(metric.get("record") or "").strip().split("\n")[0].strip() if metric else "")
