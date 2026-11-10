"""The after of a milestone deliverable made as background work: when the queued deep-harness task finishes, code measures what it made against the
deliverable's rubric (the same separate scorer the turn uses) and keeps it, with the increment before and after, on the milestone's keyed goal.

Role: the progress step hands a deliverable's work to the harness the ESOI cascade selected for work (``milestone_progress.make_progress`` with ``deep``).
In a queued deployment that is a background task, so the turn can only acknowledge it and measure the before. The task's brief carries one structured
line written by code (``work_record_line``): the quest, the milestone, the keyed goal's title, the work item, its rubric and the before. The task
executor reads that line when the task's run finishes (``TaskExecutor.execute``) and calls ``measure_on_completion``: the deliverable the worker ended its
output with (after the ``DELIVERABLE:`` line the brief asks for) is scored requirement by requirement, added to the goal's kept parts unless the worker
already kept it there, kept through ``keep_milestone_deliverable`` with the increment, and the milestone's metric is read again. Nothing here is read
from the model's words except the deliverable itself, which is what is measured.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger("quest-ai-runner.milestone-completion")

WORK_RECORD = "Milestone work record: "
DELIVERABLE_LINE = "DELIVERABLE:"
KEPT_MARKER = "Kept by Quest AI:"
RECORD_FIELDS = ("quest_id", "milestone_id", "milestone_metric_id", "title", "item", "requirements", "before", "total", "measure_name", "metric_id",
                 "method", "skill", "kind")


def work_record_line(record: Mapping[str, Any]) -> str:
    """The one structured line a deep brief carries for its completion: the fields of ``RECORD_FIELDS`` as JSON after ``WORK_RECORD``."""
    return WORK_RECORD + json.dumps({k: record[k] for k in RECORD_FIELDS if k in record}, sort_keys=True, ensure_ascii=False)


def with_work_skill(text: str, record: Mapping[str, Any], skills_root: str) -> str:
    """``text`` with an instruction to read and carry out the skill the work record names, found under this lane's ``skills_root``; ``text`` unchanged
    when no skill is named, the name is not a plain folder name, or the lane holds no such skill."""
    import os
    skill = str(record.get("skill") or "").strip()
    if not skill or not skills_root or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", skill):
        return text
    path = os.path.join(skills_root, skill, "SKILL.md")
    if not os.path.isfile(path):
        log.warning("milestone work: the skill %r is not under this lane's skills root; the plain brief runs", skill)
        return text
    return f"First read the skill at {path} (and the files it points to) and carry it out for this work.\n\n{text}"


def work_record_of(text: Any) -> Optional[Dict[str, Any]]:
    """The work record a task's text carries (the line ``work_record_line`` wrote), else None. Only that code-written line is read."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith(WORK_RECORD):
            try:
                data = json.loads(line[len(WORK_RECORD):])
            except ValueError:
                return None
            return data if isinstance(data, dict) and data.get("quest_id") and data.get("title") else None
    return None


def deliverable_of(output: Any) -> str:
    """The deliverable a worker ended its output with: the text after its last ``DELIVERABLE:`` line ("" when there is none)."""
    text = str(output or "")
    at = text.rfind(DELIVERABLE_LINE)
    return text[at + len(DELIVERABLE_LINE):].strip() if at >= 0 else ""


HEADING = re.compile(r"^(?:#{1,6}\s+\S.*|\*\*[^*\n]{2,120}\*\*:?|__[^_\n]{2,120}__:?)\s*$")


def units_by_heading(text: str) -> List[str]:
    """``[preamble, unit, unit, ...]`` when ``text`` is a set of units under headings (markdown headings or lines that are only bold), each unit with its
    heading; ``[text]`` when it holds fewer than two headed units. Structure only: the units a rubric names one by one (a script per video) are written
    under one heading each."""
    pieces: List[List[str]] = [[]]
    for line in str(text or "").splitlines():
        if HEADING.match(line.strip()):
            pieces.append([line])
        else:
            pieces[-1].append(line)
    blocks = ["\n".join(p).strip() for p in pieces]
    units = [b for b in blocks[1:] if b and len(b.splitlines()) > 1]
    return [blocks[0]] + units if len(units) > 1 else [str(text or "").strip()]


UNMET_HEAD = "Not yet meeting the rubric:"
PART_HEAD = re.compile(r"^--- Part (\d+) ---[ \t]*$", re.MULTILINE)
UNMET_LINE = re.compile(r"^- Part (\d+): (.+?)\s*$")


def unmet_section(unmet: Any) -> str:
    """The short section the kept goal carries so the NEXT run knows which requirement each part failed: ``Not yet meeting the rubric:`` then one
    ``- Part N: <requirement>`` line per failed requirement ("" when none failed). ``unmet`` is the list ``measure_on_completion`` returns
    (``{"part": N, "requirements": [...]}``). It sits in the overview, before Part 1, so ``kept_parts`` (which reads after the first part separator) never
    counts it as a part and a part's text comparison (``squash``) never meets it."""
    lines = [f"- Part {u['part']}: {' '.join(str(r).split())}" for u in unmet or [] for r in u.get("requirements") or [] if str(r).strip()]
    return ("\n\n" + UNMET_HEAD + "\n" + "\n".join(lines)) if lines else ""


def unmet_of(kept_text: Any) -> Dict[int, List[str]]:
    """``{part number: [requirement it failed, ...]}`` from the section ``unmet_section`` wrote on a kept goal ({} when it holds none)."""
    text = str(kept_text or "")
    if UNMET_HEAD not in text:
        return {}
    out: Dict[int, List[str]] = {}
    for line in text.split(UNMET_HEAD, 1)[1].lstrip("\n").splitlines():
        found = UNMET_LINE.match(line.strip())
        if not found:
            break
        out.setdefault(int(found.group(1)), []).append(found.group(2))
    return out


def numbered_parts(text: str) -> List[Tuple[int, str]]:
    """``[(N, text)]`` for each block of ``text`` that starts with a line ``--- Part N ---`` (the follow-on section excluded), in order. A block whose N is a
    kept part's number is a repair of that part; a larger N is a new part."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_progress as mp
    body = mp.FOLLOW_SPLIT.split(str(text or ""), maxsplit=1)[0]
    marks = list(PART_HEAD.finditer(body))
    found = [(int(m.group(1)), body[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(body)].strip()) for i, m in enumerate(marks)]
    return [(n, t) for n, t in found if t]


def kept_goal_text(registry: Any, record: Mapping[str, Any]) -> str:
    """The description of the milestone's keyed goal as it stands now ("" when it cannot be read or holds nothing)."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    try:
        goals = milestone_cue.goals_through(registry, str(record["quest_id"]), str(record.get("milestone_id") or "") or None)
    except milestone_cue.LaneGap as gap:
        log.warning("milestone completion: the kept goal could not be read: %s", gap)
        return ""
    goal = next((g for g in goals if str(g.get("title") or g.get("name") or "").strip() == str(record["title"]).strip()), None)
    return str((goal or {}).get("text") or (goal or {}).get("description") or "")


def sources_of(reference: str) -> str:
    """What a finished deliverable's facts may rest on: ``reference`` (the brief, then the worker's output) up to the worker's last
    ``DELIVERABLE:`` line, so the sources it read and quoted count and the deliverable itself does not."""
    text = str(reference or "")
    at = text.rfind(DELIVERABLE_LINE)
    return text[:at] if at >= 0 else text


def supported_part(part: str, sources: str, decide: Callable[[str], str]) -> Tuple[str, List[str]]:
    """``(part, removed)``: ``part`` with every fact sentence no source holds taken out (``milestone_depth.supported_sentences``, the first
    sentence of each line kept as an item's own name). The part as it was, with nothing removed, when there are no sources or the check cannot run."""
    if not sources.strip():
        return part, []
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.milestone_depth import supported_sentences
    try:
        now, removed = supported_sentences(part, sources, decide, keep_line_starts=True)
    except Exception as error:  # noqa: BLE001  a check that cannot run leaves the text as written and says so
        log.warning("milestone completion: the source check of a finished part could not run: %s: %s", type(error).__name__, error)
        return part, []
    return now.strip(), list(removed)


def measure_on_completion(record: Mapping[str, Any], output: Any, registry: Any, decide: Callable[[str], str], reference: str = "",
                          today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Measure and keep a finished background deliverable. Returns ``{"status", "before", "after", "total", "line"}``: ``measured`` with the increment
    line a reader sees, or ``no_deliverable`` / ``not_kept`` with why. ``reference`` is what the worker was given (its brief), which the scorer may check a
    requirement about the sources against.

    Which requirement each part failed is returned as ``unmet`` and kept in the goal's overview (``unmet_section``), so the next run is briefed with it. A
    deliverable block that starts with ``--- Part N ---`` for a part already kept is a repair of it: after the source check it replaces that part only when
    the scorer finds it meets strictly more requirements (``keep_better``); the result's ``repairs`` says what happened to each."""
    from hubofhubs.api import keep_better, unmet_by_unit
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_progress as mp
    from quest_ai_runner.the_world.tools.tools import ToolContext
    part = deliverable_of(output)
    if not part:
        return {"status": "no_deliverable", "line": "The finished work ended with no deliverable to measure, so nothing was kept or counted."}
    requirements = [str(r) for r in record.get("requirements") or [] if str(r).strip()]
    kept_text = kept_goal_text(registry, record)
    body_now = kept_text.split(mp.OVERVIEW_HEAD, 1)[-1] if mp.OVERVIEW_HEAD in kept_text else kept_text
    overview = body_now.split("\n--- Part", 1)[0].split(UNMET_HEAD, 1)[0].strip() if "--- Part" in body_now else ""
    part = part.split(KEPT_MARKER, 1)[-1].strip() if part.startswith(KEPT_MARKER) else part
    parts = mp.kept_parts(kept_text)
    existing = list(parts)
    requirements_given = bool(requirements)
    numbered = numbered_parts(part) if requirements_given and existing else []
    repairs: Dict[int, str] = {}
    if numbered:
        # Parts numbered as kept parts are repairs of them (they replace only when the check finds them better); a larger number is a new part.
        repairs = {n: t for n, t in numbered if 1 <= n <= len(existing)}
        fresh = [t for n, t in numbered if n > len(existing) and mp.squash(t) not in mp.squash(kept_text)]
        head = part[:PART_HEAD.search(part).start()].strip().split(mp.OVERVIEW_HEAD, 1)[-1].split(UNMET_HEAD, 1)[0].strip()
        overview = overview or head
        parts = existing + fresh
    elif mp.kept_parts(part):
        parts = mp.kept_parts(part)     # the deliverable is a whole kept body (its parts under their separators): it is the goal's text now
    elif len(units_by_heading(part)) > 1:
        overview_now, units = units_by_heading(part)[0], units_by_heading(part)[1:]
        overview = overview or overview_now
        parts = [u for u in units if mp.squash(u) not in mp.squash(kept_text)] if kept_text else units
        parts = mp.kept_parts(kept_text) + parts       # a whole set without separators: one unit per heading, each scored on its own
    elif mp.squash(part) not in mp.squash(kept_text):
        parts = parts + [part]          # the worker did not keep it on the goal itself: code keeps it as the next part
    before = float(record.get("before") or 0)
    method = "evidence" if record.get("method") == "evidence" else "rubric"
    unsupported: List[str] = []
    unmet: List[Dict[str, Any]] = []
    repaired: List[Dict[str, Any]] = []
    if method == "evidence":
        # Evidence counts the questions answered with a named source (structure, not the worker's say-so); the answers are the kept part.
        questions = list((mp.questions_of(part) or {}).get("questions") or [])
        if not questions:
            return {"status": "no_deliverable", "line": "The finished work held no questions and answers to measure, so nothing was kept or counted."}
        parts = [mp.render_questions(questions)]
        after = float(sum(1 for q in questions if q.get("answer") and q.get("source")))
        total = len(questions)
    else:
        # What the in-turn writer's text passes before it is kept, a finished background deliverable passes too: a sentence that states a fact
        # must rest on words of a source (the brief, and what the worker read and quoted before its DELIVERABLE line), or it is taken out. The
        # deliverable is never its own source. Parts already kept were checked when they were kept.
        sources = sources_of(reference)
        already = mp.squash(kept_text)
        scorer = mp.Scorer(decide, requirements, reference)      # one verdict set per distinct text, cached: each (requirement, part) is scored once
        held: List[str] = []
        for p in parts:
            if kept_text and mp.squash(p) in already:
                held.append(p)
                continue
            now, removed = supported_part(p, sources, decide)
            unsupported += removed
            if now:
                held.append(now)
        for n, text in sorted(repairs.items()):
            old = held[n - 1]
            if mp.squash(text) == mp.squash(old):
                continue
            now, removed = supported_part(text, sources, decide)       # the source check applies to the new text first
            unsupported += removed
            if not now:
                repaired.append({"part": n, "kept_new": False, "why": "nothing of the rewrite rested on a source"})
                continue
            held[n - 1], took = keep_better(old, now, requirements, scorer)
            repaired.append({"part": n, "kept_new": took, "why": "it meets more of the rubric" if took else "it does not meet more of the rubric than the part it would replace"})
        parts = held
        failed = unmet_by_unit(parts, requirements, scorer) if requirements else []
        unmet = [{"part": n, "requirements": u} for n, u in enumerate(failed, 1) if u]
        after = float(sum(1 for u in failed if not u))
        total = record.get("total") if isinstance(record.get("total"), int) else None
    day = (today or datetime.date.today()).isoformat()
    progress = mp.Progress(title=str(record["title"]), item=str(record.get("item") or ""), overview=(overview or f"{record.get('item')}.") + unmet_section(unmet), parts=parts,
                           measure_name=str(record.get("measure_name") or f"Units of {record.get('item')} meeting their requirements"),
                           before=before, after=after, total=total, as_of=day, method=method)
    increment = {"kind": "evidence" if method == "evidence" else "deliverable", "measure_id": record.get("metric_id"), "measure_name": progress.measure_name,
                 "before": before, "after": after, "total": total, "method": method, "as_of": day, "changed": after != before}
    result = registry.invoke(mp.KEEP_TOOL, {"quest_id": record["quest_id"], "title": record["title"], "body": progress.body(),
                                            "solution_id": record.get("milestone_id"), "increment": increment},
                             ToolContext(quest_id=str(record["quest_id"])))
    common = {"unsupported": unsupported, "unmet": unmet, "repairs": repaired, "before": before, "after": after, "total": total, "body": progress.body(), "increment": increment, "title": str(record["title"]),
              "measure_line": progress.increment_line(),
              "goal_found": bool(kept_text), "milestone_metric": metric_again(registry, record)}
    if not getattr(result, "ok", False):
        return dict(common, status="not_kept", line=f"The finished deliverable was measured ({progress.increment_line()}) but not kept: {str(getattr(result, 'text', ''))[:200]}")
    return dict(common, status="measured", line=progress.increment_line())


def metric_again(registry: Any, record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The milestone's metric record read again after the work was kept (its id and first line), None when it cannot be read."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    mid = str(record.get("milestone_metric_id") or "")
    try:
        tables, _ = milestone_cue.read_through(registry, str(record["quest_id"]))
    except milestone_cue.LaneGap as gap:
        log.warning("milestone completion: the metric could not be read again: %s", gap)
        return None
    metrics = dict(tables.get("metrics") or {})
    found = metrics.get(mid) or next((m for m in metrics.values() if str(record.get("milestone_id") or "") in (m.get("solution_ids") or [])), None)
    return {"id": mid, "record": str(found.get("record") or "").split("\n")[0][:300]} if isinstance(found, Mapping) else None
