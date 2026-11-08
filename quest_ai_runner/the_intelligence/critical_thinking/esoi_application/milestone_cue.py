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
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_readiness as readiness
from quest_ai_runner.the_intelligence.mind.reference_resolver import quest_references

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
    milestone's name or the label its name starts with ("P5" for "P5 Foundational product videos") and no other option shares it, that milestone. NONE is a verdict
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
              "preparing for a call, or information alone (how something stands, with nothing asked to be done). "
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


IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]")


def truncated_tokens(text: str, allowed: str) -> List[str]:
    """The identifier-like tokens of ``text`` (they hold a hyphen, an underscore or a dot between characters) that are not a token of ``allowed`` but are the start of one: a cut identifier
    (a source name that lost its end). Plain words are not looked at, since a word is often the start of a longer one."""
    known = {t.lower() for t in IDENTIFIER_TOKEN.findall(allowed or "")}
    return [t for t in IDENTIFIER_TOKEN.findall(text or "")
            if re.search(r"[A-Za-z0-9][_.-][A-Za-z0-9]", t) and t.lower() not in known and any(k.startswith(t.lower()) for k in known)]


GROUNDING_CHARS = 6000      # how much of the turn's reads and context the draft call is shown


def supported_draft(draft: str, allowed: str) -> str:
    """The draft without any line that states a figure the records do not hold (a count of its own characters, an invented rate): a figure outside the milestone's record text is
    unsupported and its line is dropped, so a draft never carries a number that came from nowhere. List numbering and a [bracket] are not figures."""
    from quest_ai_runner.the_intelligence.mind.response_writer.interim_policy import FIGURE
    held = set(FIGURE.findall(allowed or ""))          # whole figures only: 12 is not held because 2026-12-31 contains it
    kept = []
    for line in str(draft or "").splitlines():
        body = re.sub(r"^\s*(?:[-*\u2022]\s*)?\d+[.)]\s+", "", line)
        body = re.sub(r"\[[^\]]*\]", "", body)
        if any(f not in held for f in FIGURE.findall(body)) or truncated_tokens(body, allowed):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def draft_of(goal_text: str) -> str:
    """The draft a keyed goal already holds in its description (after its ``Draft:`` line), else "": a repeat reuses it, so it never rewrites the goal."""
    head, sep, rest = str(goal_text or "").partition("Draft:")
    return rest.strip() if sep else ""


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


def with_value_line(description: str, line: str) -> str:
    """``description`` holding exactly one ``Current value:`` line, ``line``: an existing one is replaced in place, a missing one is added after the first line, never a second."""
    lines = str(description or "").split("\n")
    kept, placed = [], False
    for text in lines:
        if text.startswith(CURRENT_VALUE):
            if not placed:
                kept.append(line)
                placed = True
            continue
        kept.append(text)
    if not placed:
        kept.insert(1 if kept else 0, line)
    return "\n".join(kept)


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


INCREMENT_ORDER = ("Increment order, the first that applies (one increment per turn): "
                   "1. a deliverable the milestone needs that you can write from what you have: write it in the reply (deep_target draft_in_chat) and record it as a goal keyed "
                   "{title} followed by a short name; "
                   "2. else a metric value you can read from the data your tools reach: record it with the metric's record id and its source and date (when the facts name a metric source, code reads it and records the value in the keyed next step); "
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
    source: str = ""                                     # the name of the metric source the record gives ("" when it names none)
    reading: Optional[Mapping[str, Any]] = None          # what read_metric_source returned: value, as_of, source (label), detail
    reading_gap: str = ""                                # why the source could not be read, in the tool's words
    kept_line: str = ""                                  # the Current value line the keyed goal already holds
    stage_before: Optional[readiness.Stage] = None       # the readiness stage of the next step as the quest held it before the write
    stage_after: Optional[readiness.Stage] = None        # the same, from the goal as the write read it back
    owner_label: str = ""                                # who owns the step, as the milestone's record names them (with the account name when it differs)
    people: Tuple[str, ...] = ()                         # the people the milestone text names, as it writes them
    grounding: str = ""                                  # what the turn read and was shown: the only source for a draft's claim about a product or past work
    human_step: str = ""                                 # the step only a person can do, as kept in the goal
    value_full: str = ""                                 # the metric record's whole text (whitespace collapsed): what a draft is written from, never a cut of it
    records_text: str = ""                               # the milestone's record and its metric records as the code read them
    record_date: str = ""                                # the day the metric record's own reading is from (its measured_through, else a day in its text)
    record_line: str = ""                                # the metric record's first line as written: a dated "Current value: ... (as of ..., from ...)" is a recorded reading

    def routes_to_code(self, plan: Any, user_intent: Optional[str], gathered: Sequence[Mapping[str, Any]], registry: Any) -> bool:
        """True when the person asked for something to be done (the planner's ``user_intent``), no tool has already changed something this turn (a refused or blocked write has not),
        and the plan is one the flow step takes over. A deliverable the planner means to write in the reply does not exempt it: the step records the owned next step with its own draft, so a
        reply that fails to record anything still ends with the step kept. Structured fields only."""
        return (not self.receipt and user_intent == ACT and getattr(plan, "action", None) in CODE_ROUTED_ACTIONS
                and not applied_write_ran(gathered, registry))

    def week_goal(self, goal_id: str = "") -> Optional[Mapping[str, Any]]:
        """The goal that is (or will be updated as) this milestone's next step: the keyed goal of this week, else the one the model chose to adopt, else a goal this flow wrote that is
        linked to the milestone. None when there is none yet."""
        from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
        title = next_step_title(self.name, self.week)
        return (next((g for g in self.candidates if str(g.get("title") or "").strip() == title), None)
                or next((g for g in self.candidates if goal_id and str(g.get("id")) == str(goal_id)), None)
                or next((g for g in self.candidates if str(g.get("spec_solution_id") or "") == self.rid and is_owned(g)), None))

    def measure_stage(self, goal: Optional[Mapping[str, Any]]) -> readiness.Stage:
        return readiness.measure(goal, owners=self.owners, solution_id=self.rid, metric_ids=self.metric_ids, monday=self.week.monday, due=self.week.due)

    def doing(self) -> str:
        """What happens now, for the first message: stated from the records, before the write, with the readiness of the step as the quest holds it."""
        week = next_step_title(self.name, self.week)
        exists = any(str(g.get("title") or "").strip() == week for g in self.candidates)
        stage = self.measure_stage(self.week_goal())
        return ((f"Updating this week's next step for {self.name}, which is already on the quest, and keeping its owner and draft." if exists
                 else f"Recording this week's next step for {self.name}, linked to the milestone, with its owner.")
                + f" Readiness of the step now: {stage.describe()}.")

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
        record's text, the source's reading and the goal as it stood before and after the write."""
        def entry(tool: str, text: str) -> Dict[str, Any]:
            return {"kind": "query", "locator": f"tool:{tool}", "text": text, "tool_status": "applied", "by_code": True}
        out = []
        if self.records_text:
            out.append(entry("read_spec_records", self.records_text))
        if self.reading:
            out.append(entry(READ_TOOL, current_value_line(self.reading["value"], self.reading["as_of"], self.reading["source"])
                             + (f"\nDetail: {self.reading['detail']}" if self.reading.get("detail") else "")))
        goal = self.week_goal()
        if goal:
            out.append(entry("list_quest_goals", goal_summary(goal) + "\n" + str(goal.get("text") or "")))
        if self.after_state:
            out.append(entry("list_quest_goals", "After the write: " + ", ".join(f"{k} {v}" for k, v in self.after_state.items() if v not in (None, ""))))
        return out

    def related_text(self) -> str:
        """The person's own goals for this milestone are linked to only by being named here, in this goal's description; they are never changed."""
        from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
        titles = [str(g.get("title")) for g in self.candidates if g.get("title") and not is_owned(g)][:4]
        return ("\nRelated goals on the quest: " + "; ".join(titles) + ".") if titles else ""

    def goal_arguments(self) -> Dict[str, Any]:
        # A source the record names that could not be read puts no figure on the goal: the line says so, and the older reading (the goal's own, else the record's) is not
        # passed off as the current value.
        unread = f"{CURRENT_VALUE} not read, the {self.source} source could not be read just now"
        line = (current_value_line(self.reading["value"], self.reading["as_of"], self.reading["source"]) if self.reading
                else self.kept_line or (unread if self.source else f"{CURRENT_VALUE} {self.value or 'not recorded yet'}"))
        args: Dict[str, Any] = {
            "quest_id": self.quest_id, "title": next_step_title(self.name, self.week), "period": self.week.period, "due_date": self.week.due,
            "description": with_value_line(f"Next step for {self.name}. Done when: {self.done_when}.", line) + self.related_text(),
            "spec_solution_id": self.rid, "match_by_link": True, "user_asked_for_this_field": True}
        if self.metric_ids:
            args["spec_metric_id"] = self.metric_ids[0]
        return args

    LEADS = {"created": "I created this week's next step for {name}.",
             "updated": "I updated the existing next step for {name} in place (no second goal was made).",
             "unchanged": "This week's next step for {name} was already set, so nothing was changed."}

    def lead_sentence(self, state: str) -> str:
        """The plain statement of what the write did, composed by code from the tool's typed state: created, updated in place, or already set; then who owns it, by the name the
        milestone's record uses (with the account's name beside it when that differs)."""
        lead = self.LEADS.get(state, "I recorded this week's next step for {name}.").format(name=self.name)
        lead = f"{lead} {self.owner_label} owns it." if self.owner_label else lead
        # The step's own due date, said by code: left to the reply, the milestone's target date was given as the step's due date.
        due = str((self.after_state or {}).get("due_date") or self.week.due or "")[:10]
        try:
            day = datetime.date.fromisoformat(due)
        except ValueError:
            return lead
        return f"{lead} The step is due {day.strftime('%A')} {day.day} {day.strftime('%B')}."

    DRAFT_FORM = ('"draft": the deliverable itself, never a plan to write it, written to be used as it is and short enough to send or use (under 120 words). Its type comes from what the milestone '
                  'needs, read from its done-when, what it produces and its text: for a milestone that depends on an outside party, a short message to that party stating what they get, the ask and '
                  'the next step (a partner pitch); for a milestone about a video, the words to be spoken, not notes about it; for copy to be compared, distinct alternatives with the metric they are '
                  'compared by; for people to be asked, only a few neutral questions, each a full sentence ready to send, with no greeting, introduction or message around them; for a new source or channel, exactly one source (never a choice of two or more) that the facts above name (when they name none, the kind of source to '
                  'try first, labelled as a candidate with why) with a one-line hypothesis, a first test and the milestone metric; for a release or an announcement, the message to be published; a checklist of at most 5 steps only when none of these fits. '
                  'Use only the facts above: never a person, place, group, platform or organisation they do not name; never a claim that some work was done or a result came out, or about what a product, service or organisation is, does, has or offers, unless the facts above state it (a script or message about a product says only what the facts above say of it); never state a figure that contradicts the current value above; no prices, dates, counts, rates or claims that are not given above, and never a count of its own characters, words or items; no first person '
                  '("I", "we", "our") unless the facts above name who is speaking; never a due date or deadline that is not in the facts above; no em dashes. '
                  'Leave no bracket slot, no angle slot and no blank to fill ("[source name]", "[link]"): fill each from the facts above and the reads; where a fact is unknown, name one concrete '
                  'candidate the records support, labelled as a candidate with why, or write the deliverable so it is sendable as it is (for example neutral questions to the person who knows)')

    def asks_for_fix(self, draft: str, decide: Callable[[str], str]) -> str:
        """A draft that still holds a slot to fill is sent back once to be fixed; a second draft that still holds one is dropped (the goal then keeps none and its readiness says so)."""
        slots = readiness.placeholder_slots(draft)
        if not slots:
            return draft
        answer = decide(f"Milestone: {self.name}. Done when: {self.done_when}.\n\nThe milestone text:\n{clip(self.purpose, PURPOSE_CHARS) or '(none)'}\n\nThis draft has slots left to fill: "
                        + ", ".join(slots) + f"\n\nDraft:\n{draft}\n\nReturn JSON with this key only: " + self.DRAFT_FORM + ". Reply with the JSON only.")
        fixed = supported_draft(str(parse_json(answer).get("draft") or "").strip(), self.allowed_text())
        return "" if readiness.placeholder_slots(fixed) else fixed

    def choices(self, decide: Optional[Callable[[str], str]]) -> Dict[str, Any]:
        """The things that need reading the record text, in ONE cheap call: ``owner`` (when the record's ``tracking_owner`` names nobody, the person the purpose text names as
        doing the work), ``goal_id`` (the keyed next-step goal on the quest that is already this milestone's next step, any week, any older title; only keyed goals are offered,
        a person's goal never is), ``draft`` (the deliverable for the step, kept in the goal; not asked again when the existing keyed goal already holds one without a slot left to
        fill) and ``human_step`` (the one step only a person can do, named in the goal)."""
        from quest_ai_runner.the_body.hands.quest_operations.goal_links import is_owned
        keyed = [g for g in self.candidates if is_owned(g)]       # goals this flow wrote earlier only; a person's goal is never offered
        if decide is None:
            return {"owners": [], "goal_id": "", "draft": "", "human_step": ""}
        this_week = next((g for g in keyed if str(g.get("title") or "").strip() == next_step_title(self.name, self.week)), None)
        kept_text = str((this_week or {}).get("text") or "")
        kept_draft = draft_of(kept_text)
        if readiness.placeholder_slots(kept_draft):
            kept_draft = ""          # a kept draft with a slot left in it is written again
        kept_step = readiness.human_step_in(kept_text)
        listing = "\n".join(f"{g.get('id')}: {g.get('title')} | {clip(g.get('text'), 200)}" for g in keyed) or "(none)"
        asks = []
        if not self.owners and self.purpose.strip():
            asks.append('"owners": every person named as an owner in the milestone text above, as written (names only, no team names), the one who does the hands-on work first, or [] when it names none')
        if keyed:
            asks.append('"goal_id": the id, exactly as listed, of the open goal below that is already the next step of this milestone (the same work, whatever its title or week), or ""')
        if not kept_draft:
            asks.append(self.DRAFT_FORM)
        if not kept_step:
            asks.append('"human_step": the one next step only a person can do toward this milestone, one full sentence from the done-when and the text above, naming what is done and by whom when the text says')
        if asks and len(self.owners) == 1 and self.purpose.strip():       # asked in the same call, only when the call is made anyway
            asks.append('"people": every person the milestone text above names, as the text writes the name (no team names, no roles), or []')
        if not asks:
            return {"owners": [], "goal_id": "", "draft": kept_draft, "human_step": kept_step}
        outputs = clip(self.row.get("outputs"), PURPOSE_CHARS)
        answer = decide(f"Milestone: {self.name}. Done when: {self.done_when}. Current value: {self.current_text()}."
                        + (f" The metric record's older text, history only and not the current value: {self.value}." if self.reading and self.value else "")
                        + f"\n\nThe milestone text:\n{clip(self.purpose, PURPOSE_CHARS) or '(none)'}\n\n"
                        + (f"What the milestone produces:\n{outputs}\n\n" if outputs else "")
                        + f"Open goals on the same quest:\n{listing}\n\n"
                        + (f"What this turn read and was shown (a fact here counts as a fact above):\n{clip(self.grounding, GROUNDING_CHARS)}\n\n" if self.grounding.strip() else "")
                        + "Return JSON with these keys only: " + "; ".join(asks) + ". Reply with the JSON only.")
        data = parse_json(answer)
        goal = str(data.get("goal_id") or "")
        named = data.get("owners") if isinstance(data.get("owners"), list) else [data.get("owner")]
        draft = kept_draft or supported_draft(str(data.get("draft") or "").strip(), self.allowed_text())
        if draft and not kept_draft:
            draft = self.asks_for_fix(draft, decide)
        if draft and not kept_draft:
            # Before the draft is kept in the goal, independent checks hold each claim in it to the facts it was written from: a claim nothing supports is rewritten from
            # them or removed (the reply check sees the reply only after the goal is written).
            from quest_ai_runner.the_intelligence.critical_thinking.decision_check.reply_views import grounded_text
            draft = grounded_text(draft, self.allowed_text() + "\n" + self.grounding, decide)
        step = kept_step or " ".join(str(data.get("human_step") or "").split())
        people = data.get("people") if isinstance(data.get("people"), list) else []
        self.people = tuple(dict.fromkeys(str(n).strip(" .'\"") for n in people if str(n).strip(" .'\"")))
        return {"owners": [str(n).strip(" .'\"") for n in named if n and str(n).strip(" .'\"")], "goal_id": quest_references.goal_offered(goal, keyed),      # an id the model was not offered is never adopted
                "draft": draft, "human_step": step}

    def name_owner(self, shown: Mapping[str, Any], decide: Optional[Callable[[str], str]]) -> str:
        """Who owns the step, as a reader recognises them: the name the milestone's record uses for the person, with the account's own name in parentheses when it differs ("Ann (account
        Ann Ops)"). An account name sharing a whole word with the record's text is the person. When the record lists one owner and its text names one person, that is the name, with no
        model call. Otherwise one cheap call asks which name in the text is theirs, and an answer that is not a name in the text is discarded (the account name alone is then shown)."""
        account = str(shown.get("assignee_name") or "").strip()
        if not account:
            return ""
        whole = lambda text: {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) > 2}
        if whole(account) & whole(self.purpose):
            return account
        # Only the people the milestone text itself names count: a name the draft call made up (a contact it invented) is not a candidate for the owner.
        named = [p for p in self.people if p.lower() in self.purpose.lower()]
        if len(self.owners) == 1 and len(named) == 1 and not whole(named[0]) & whole(account):
            return f"{named[0]} (account {account})"
        if decide is None:
            return account
        try:
            answer = " ".join(str(decide(f"Milestone text:\n{clip(self.purpose, PURPOSE_CHARS)}\n\nWhich name in this text refers to the person whose Quest account is named {account!r}? "
                                         "Reply with that name exactly as the text writes it, or NONE if no name in the text is theirs.") or "").split()).strip(" .'\"")
        except Exception:  # noqa: BLE001  a name is a convenience for the reader; the account name stands
            return account
        return f"{answer} (account {account})" if answer and answer != NONE and answer.lower() in self.purpose.lower() and answer.lower() != account.lower() else account

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

    def reading_note(self) -> str:
        """What the receipt tells the reply about the measured value."""
        if self.reading:
            line = current_value_line(self.reading["value"], self.reading["as_of"], self.reading["source"])
            stood = (f" Its source could not be read again just now ({self.reading_gap}), so the recorded reading stands; say so in a few words."
                     if self.reading_gap else "")
            return (f" The metric's current value was read and the goal's one line now reads: \"{line}\". "
                    "State the value, the date it covers and the source label in the reply, exactly as that line gives them, and no other figure for it." + stood)
        if self.source:
            return f" The metric's value could not be read ({self.reading_gap}). Say that, say which data is missing, and state no value for it."
        return ""

    KEPT_READING = re.compile(r"^Current value:\s*(?P<value>.*?)\s*\(as of (?P<day>[^,)]+), from (?P<source>.*)\)\s*$")

    def metric_sentence(self, before_value: str, after_value: str) -> str:
        """The milestone value before and after, each from where it came and only ever the same measure against itself. With a source: the goal's previous ``Current value:`` line
        (same source) against the reading now; with no previous line the step had none, and the metric record's own last reading is given labelled as such, not as a before. With no
        source the metric record's value is read twice, and an unchanged one is said as unchanged, with why."""
        if self.reading:
            from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import shown
            now, day, source = shown(self.reading["value"]), self.reading["as_of"], self.reading["source"]
            earlier = self.KEPT_READING.match(self.kept_line or "")
            record = f"the metric record's last reading: {before_value}" + (f" ({self.record_date})" if self.record_date else "")
            if earlier and earlier.group("source").strip() == str(source).strip():
                if earlier.group("value") == now:
                    return f"unchanged at {now} (as of {day}, from {source}), because the source still reads the same as the goal's previous line ({earlier.group('day')})"
                return f"was {earlier.group('value')} (as of {earlier.group('day')}), now {now} (as of {day}), both from {source}"
            if earlier:
                return f"now {now} (as of {day}, from {source}); the goal's previous line came from another source ({earlier.group('source')}), so it is not compared; {record}"
            return f"now {now} (as of {day}, from {source}); the step had no earlier reading of this measure; {record}"
        if not (before_value and after_value):
            return "not recorded, so no before and after can be shown"
        if before_value == after_value:
            why = "it changes only when a person completes the step above" if self.human_step else "nothing recorded has moved it"
            return f"unchanged at {before_value} (the metric record), because {why}"
        return f"was {before_value}, now {after_value} (the metric record)"

    def closing(self, before_value: str, after_value: str) -> str:
        """The one line message 2 ends with once the step is recorded: the readiness stage before and after, and the milestone metric before and after (said as unchanged, with why,
        when it did not move). "" when the step was not recorded."""
        if self.stage_before is None or self.stage_after is None:
            return ""
        return readiness.closing_line(self.stage_before, self.stage_after, label=self.name, metric=self.metric_sentence(before_value, after_value))

    def apply_owner_label(self, text: str) -> str:
        """The reply naming the owner as the record names them: where the reply writes the account's own name alone, the name with the account beside it ("Ann (account Ann Ops)").
        Text that already carries the label, or an owner whose account name is the name, is returned as it was."""
        account = str((self.after_state or {}).get("assignee_name") or "").strip()
        if not self.owner_label or not account or self.owner_label == account or self.owner_label in str(text or ""):
            return text
        return re.sub(re.escape(account) + r"(?! \(account)", self.owner_label.replace("\\", "\\\\"), str(text or ""))

    def without_questions(self, text: str) -> str:
        """The reply without a closing paragraph that asks the person something: a paragraph outside the kept draft whose last character is a question mark. The draft's own questions (neutral
        questions for someone else) stay; they are the deliverable. The reply keeps whatever else it said; text with nothing else in it is returned as it was."""
        draft_lines = {re.sub(r"^\s*(?:>|[-*\u2022]|\d+[.)])\s*", "", line).strip() for line in self.draft.splitlines() if line.strip()}
        kept = []
        for paragraph in re.split(r"\n\s*\n", str(text or "")):
            lines = [re.sub(r"^\s*(?:>|[-*\u2022]|\d+[.)])\s*", "", line).strip() for line in paragraph.splitlines() if line.strip()]
            in_draft = bool(lines) and all(line in draft_lines for line in lines)
            if paragraph.rstrip().endswith("?") and not in_draft:
                continue
            kept.append(paragraph)
        out = "\n\n".join(kept).strip()
        return out or str(text or "")

    def carry_out(self, registry: Any, decide: Optional[Callable[[str], str]] = None, grounding: str = "") -> Tuple[str, str]:
        """The keyed idempotent write, then its receipt: ``(receipt text, the tool's typed status)``. A goal already doing this milestone's next step under an older title is
        updated in place (renamed to the key, linked, moved to the week), never duplicated; a repeat finds the keyed goal and says it is already there. Each owner is tried as
        assignee in turn; with none that Quest accepts the step is written unassigned and the receipt says so. A tool the lane lacks, or a write that failed or is held, is a
        ``LaneGap`` carrying the tool's own words. ``decide(prompt) -> text`` is one cheap model call used for the two choices that need reading the record text."""
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if registry is None or registry.get("create_or_update_quest_goal") is None:
            raise LaneGap("this lane has no create_or_update_quest_goal tool, so the next step could not be recorded")
        ctx = ToolContext(quest_id=self.quest_id)
        self.grounding = str(grounding or "")       # what the turn read and was shown: the only source a draft may take a claim about a product or past work from
        self.read_source(registry)
        base = self.goal_arguments()
        try:
            picked = self.choices(decide)
        except Exception as error:      # noqa: BLE001  the choice failed: surfaced, not guessed
            raise LaneGap(f"choosing the owner or the existing goal failed ({type(error).__name__}: {error})") from error
        owners = list(self.owners) or list(picked["owners"])
        if picked["goal_id"]:
            base["adopt_goal_id"] = picked["goal_id"]
        self.stage_before = self.measure_stage(self.week_goal(picked["goal_id"]))
        if picked["human_step"]:
            base["description"] = base["description"] + f"\n{readiness.HUMAN_STEP} {picked['human_step']}"
            self.human_step = picked["human_step"]
        if picked["draft"]:
            base["description"] = base["description"] + f"\n{readiness.DRAFT} {picked['draft']}"
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
                written = dict(self.after_state or {}, description=base["description"])
                written["scheduled_date"] = written.get("scheduled_date") or base.get("due_date")
                self.stage_after = self.measure_stage(written)
                self.owner_label = self.name_owner(self.after_state or {}, decide)
                named = ", ".join(owners)
                note = (f" The owner could not be set ({'; '.join(refused)}), so the goal is left unassigned. Say plainly that the record names {named} but the quest cannot assign the goal to them, say who should own it, and ask nothing."
                        if refused and not owner else "")
                lead = self.lead_sentence(str((result.data or {}).get("state") or ""))
                if (result.data or {}).get("problems"):
                    note += " The quest does not show everything that was asked for (" + "; ".join(result.data["problems"]) + "): say exactly that, and do not say it is linked or owned."
                note += self.reading_note()
                if self.human_step:
                    note += f" The step only a person can do, recorded in the goal for its owner: {self.human_step} State it in the reply."
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


def records_read(tables: Mapping[str, Mapping[str, Mapping[str, Any]]], rid: str, row: Mapping[str, Any]) -> str:
    """The milestone's record and its metric records as the code read them, in words a reader of the turn's reads can check a figure or a date against."""
    parts = [f"Milestone record: {row.get('name') or rid}. Done when: {str(row.get('milestones') or '').strip()}. Purpose: {str(row.get('purpose') or '').strip()}. "
             f"Outputs: {str(row.get('outputs') or '').strip()}. Measures of success: {str(row.get('measures_of_success') or '').strip()}."]
    for _, metric in metrics_of(tables, rid):
        parts.append(f"Metric record: {metric.get('name')}. Recorded: {str(metric.get('record') or '').strip()}. Source: {metric.get('source') or ''}. "
                     f"Measured through: {metric.get('measured_through') or ''}.")
    return "\n".join(parts)


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
    goals = goals_through(registry, quest_id)
    linked = metrics_of(tables, rid)
    metric = linked[0][1] if linked else None
    return Resolved(
        rid=rid, name=str(row.get("name") or rid), row=row, metric_ids=[mid for mid, _ in linked], owners=owners_of(row), week=week,
        facts=fact_block(tables, cue, week.monday, goals, zone, rid), quest_id=quest_id,
        done_when=clip(row.get("milestones"), VALUE_CHARS).rstrip("."),
        value=first_sentence(str(metric.get("record") or "")) if metric else "",
        value_full=" ".join(str(metric.get("record") or "").split()) if metric else "",
        purpose=str(row.get("purpose") or ""), candidates=candidate_goals(goals, rid, str(row.get("name") or rid)), source=source_of(tables, rid, row), records_text=records_read(tables, rid, row),
        record_date=measured_through_of(tables, rid, row) or next(iter(re.findall(r"\d{4}-\d{2}-\d{2}", str(metric.get("record") or ""))), "") if metric else "",
        record_line=str(metric.get("record") or "").strip().split("\n")[0].strip() if metric else "")
