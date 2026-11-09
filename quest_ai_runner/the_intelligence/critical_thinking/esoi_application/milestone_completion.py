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
from typing import Any, Callable, Dict, Mapping, Optional

log = logging.getLogger("quest-ai-runner.milestone-completion")

WORK_RECORD = "Milestone work record: "
DELIVERABLE_LINE = "DELIVERABLE:"
RECORD_FIELDS = ("quest_id", "milestone_id", "title", "item", "requirements", "before", "total", "measure_name", "metric_id")


def work_record_line(record: Mapping[str, Any]) -> str:
    """The one structured line a deep brief carries for its completion: the fields of ``RECORD_FIELDS`` as JSON after ``WORK_RECORD``."""
    return WORK_RECORD + json.dumps({k: record.get(k) for k in RECORD_FIELDS}, sort_keys=True, ensure_ascii=False)


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
    parts = mp.kept_parts(kept_text)
    if mp.squash(part) not in mp.squash(kept_text):
        parts = parts + [part]          # the worker did not keep it on the goal itself: code keeps it as the next part
    scorer = mp.Scorer(decide, requirements, reference)
    met = [p for p in parts if requirements and all(scorer(r, p) for r in requirements)]
    before = float(record.get("before") or 0)
    after = float(len(met))
    total = record.get("total") if isinstance(record.get("total"), int) else None
    day = (today or datetime.date.today()).isoformat()
    progress = mp.Progress(title=str(record["title"]), item=str(record.get("item") or ""), overview=overview or f"{record.get('item')}.", parts=parts,
                           measure_name=str(record.get("measure_name") or f"Units of {record.get('item')} meeting their requirements"),
                           before=before, after=after, total=total, as_of=day, method="rubric")
    result = registry.invoke(mp.KEEP_TOOL, {"quest_id": record["quest_id"], "title": record["title"], "body": progress.body(),
                                            "solution_id": record.get("milestone_id"),
                                            "increment": {"kind": "deliverable", "measure_id": record.get("metric_id"), "measure_name": progress.measure_name,
                                                          "before": before, "after": after, "total": total, "method": "rubric", "as_of": day,
                                                          "changed": after != before}},
                             ToolContext(quest_id=str(record["quest_id"])))
    if not getattr(result, "ok", False):
        return {"status": "not_kept", "before": before, "after": after, "total": total,
                "line": f"The finished deliverable was measured ({progress.increment_line()}) but not kept: {str(getattr(result, 'text', ''))[:200]}"}
    return {"status": "measured", "before": before, "after": after, "total": total, "line": progress.increment_line()}
