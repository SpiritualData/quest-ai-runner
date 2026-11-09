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
from typing import Any, Callable, Dict, List, Mapping, Optional

log = logging.getLogger("quest-ai-runner.milestone-completion")

WORK_RECORD = "Milestone work record: "
DELIVERABLE_LINE = "DELIVERABLE:"
KEPT_MARKER = "Kept by Quest AI:"
RECORD_FIELDS = ("quest_id", "milestone_id", "milestone_metric_id", "title", "item", "requirements", "before", "total", "measure_name", "metric_id",
                 "method")


def work_record_line(record: Mapping[str, Any]) -> str:
    """The one structured line a deep brief carries for its completion: the fields of ``RECORD_FIELDS`` as JSON after ``WORK_RECORD``."""
    return WORK_RECORD + json.dumps({k: record[k] for k in RECORD_FIELDS if k in record}, sort_keys=True, ensure_ascii=False)


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


def measure_on_completion(record: Mapping[str, Any], output: Any, registry: Any, decide: Callable[[str], str], reference: str = "",
                          today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Measure and keep a finished background deliverable. Returns ``{"status", "before", "after", "total", "line"}``: ``measured`` with the increment
    line a reader sees, or ``no_deliverable`` / ``not_kept`` with why. ``reference`` is what the worker was given (its brief), which the scorer may check a
    requirement about the sources against."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_progress as mp
    from quest_ai_runner.the_world.tools.tools import ToolContext
    part = deliverable_of(output)
    if not part:
        return {"status": "no_deliverable", "line": "The finished work ended with no deliverable to measure, so nothing was kept or counted."}
    requirements = [str(r) for r in record.get("requirements") or [] if str(r).strip()]
    kept_text = kept_goal_text(registry, record)
    body_now = kept_text.split(mp.OVERVIEW_HEAD, 1)[-1] if mp.OVERVIEW_HEAD in kept_text else kept_text
    overview = body_now.split("\n--- Part", 1)[0].strip() if "--- Part" in body_now else ""
    part = part.split(KEPT_MARKER, 1)[-1].strip() if part.startswith(KEPT_MARKER) else part
    parts = mp.kept_parts(kept_text)
    if mp.kept_parts(part):
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
    if method == "evidence":
        # Evidence counts the questions answered with a named source (structure, not the worker's say-so); the answers are the kept part.
        questions = list((mp.questions_of(part) or {}).get("questions") or [])
        if not questions:
            return {"status": "no_deliverable", "line": "The finished work held no questions and answers to measure, so nothing was kept or counted."}
        parts = [mp.render_questions(questions)]
        after = float(sum(1 for q in questions if q.get("answer") and q.get("source")))
        total = len(questions)
    else:
        scorer = mp.Scorer(decide, requirements, reference)
        met = [p for p in parts if requirements and all(scorer(r, p) for r in requirements)]
        after = float(len(met))
        total = record.get("total") if isinstance(record.get("total"), int) else None
    day = (today or datetime.date.today()).isoformat()
    progress = mp.Progress(title=str(record["title"]), item=str(record.get("item") or ""), overview=overview or f"{record.get('item')}.", parts=parts,
                           measure_name=str(record.get("measure_name") or f"Units of {record.get('item')} meeting their requirements"),
                           before=before, after=after, total=total, as_of=day, method=method)
    increment = {"kind": "evidence" if method == "evidence" else "deliverable", "measure_id": record.get("metric_id"), "measure_name": progress.measure_name,
                 "before": before, "after": after, "total": total, "method": method, "as_of": day, "changed": after != before}
    result = registry.invoke(mp.KEEP_TOOL, {"quest_id": record["quest_id"], "title": record["title"], "body": progress.body(),
                                            "solution_id": record.get("milestone_id"), "increment": increment},
                             ToolContext(quest_id=str(record["quest_id"])))
    common = {"before": before, "after": after, "total": total, "body": progress.body(), "increment": increment, "title": str(record["title"]),
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
