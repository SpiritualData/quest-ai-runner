"""What a milestone turn reads and keeps, as plain data: the shared constants, JSON and text helpers, the record and kept-goal readers and the spec-edit writer.

Role: the base of the milestone-progress modules. Nothing here judges or writes a deliverable: it names the tools and separators the kept goal uses, reads the kept parts, follow-on and evidence questions back from a goal, reads the whole milestone row and its metric reading fields, and writes records through ``apply_spec_edit``. Every other ``milestone_*`` progress module imports from here, and this one imports none of them.
"""
from __future__ import annotations

import json
import logging
import re

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


log = logging.getLogger("quest-ai-runner.milestone-progress")


KEEP_TOOL = "keep_milestone_deliverable"


SPEC_EDIT_TOOL = "apply_spec_edit"


OUTWARD = "outward_act"


DELIVERABLES = "deliverables"


PART_SEPARATOR = "\n\n--- Part {n} ---\n"


PART_SPLIT = re.compile(r"\n*--- Part \d+ ---\n")


FOLLOW_SEPARATOR = "\n\n--- Then, depending on it: {item} ---\n"


FOLLOW_SPLIT = re.compile(r"\n*--- Then, depending on it: (?P<item>.+?) ---\n")


OVERVIEW_HEAD = "Overview:\n"


GROUNDING_CHARS = 12000


DEPTH_CALLS = 16       # the depth loop's call budget for one unit (work and check calls; the hub default)


RECORD_CHARS = 1500


NO_EM_DASH = "Do not use em dashes."


TO_BE_DECIDED = "to be decided"


class ToolSpecEdit:
    """ESOI Application at the turn's ``apply_spec_edit`` tool: the hub's upkeep writes the intermediate metric through it, under Quest's own approval rules."""

    def __init__(self, registry: Any, quest_id: str):
        self.registry, self.quest_id = registry, quest_id
        self.written: List[Dict[str, Any]] = []       # every edit this turn wrote or proposed, in order, so the reply names each kept artifact by code

    def read(self, subject: str = "") -> Mapping[str, Any]:
        return {}

    def apply(self, change: Mapping[str, Any]) -> Mapping[str, Any]:
        from quest_ai_runner.the_world.tools.tools import ToolContext
        if self.registry is None or self.registry.get(SPEC_EDIT_TOOL) is None:
            return {"applied": False, "reason": f"this lane has no {SPEC_EDIT_TOOL} tool"}
        edit = dict(change.get("edit") or change)
        if edit.get("kind") == "record_add" and edit.get("table") == "metrics":
            edit["fields"] = quest_metric_fields(edit.get("fields") or {})
        result = self.registry.invoke(SPEC_EDIT_TOOL, {"quest_id": self.quest_id, "change": edit}, ToolContext(quest_id=self.quest_id))
        data = dict(getattr(result, "data", None) or {}) if isinstance(getattr(result, "data", None), Mapping) else {}
        if getattr(result, "ok", False):
            self.written.append({"edit": edit, "state": "proposed" if data.get("proposed") and not data.get("applied", True) else "kept"})
            return dict({"applied": True}, **data)
        if data.get("proposed"):
            self.written.append({"edit": edit, "state": "proposed"})
        return dict({"applied": False, "reason": str(getattr(result, "text", ""))[:300]}, **data)


#: The columns Quest keeps on a metric record that a new one may set (``parent_metric_ids`` is retired there: the link to the parent is the
#: ``cascading_conditionals`` line, which states it).
QUEST_METRIC_COLUMNS = ("name", "purpose", "record", "cascading_conditionals", "solution_ids")


def quest_metric_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """An intermediate metric's fields as Quest stores them: its own columns, with what is counted (the unit, the total and what each unit must meet) said in
    its purpose, so a person reading the record sees the definition whole."""
    out = {k: v for k, v in fields.items() if k in QUEST_METRIC_COLUMNS}
    rubric = [str(r) for r in fields.get("rubric") or [] if str(r).strip()]
    extra = (f" Counted out of {fields['total']} {fields.get('unit') or 'units'}." if fields.get("total") else "") + (
        " Each counts when it: " + "; ".join(rubric) + "." if rubric else "")
    if extra:
        out["purpose"] = (str(out.get("purpose") or "").rstrip() + extra).strip()
    return out


def parse_json(text: Any) -> Dict[str, Any]:
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def squash(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def kept_follow_on(text: str) -> Tuple[str, str]:
    """``(item, unit)`` of the follow-on unit a kept deliverable goal holds after its parts, else ``("", "")``."""
    pieces = FOLLOW_SPLIT.split(str(text or ""), maxsplit=1)
    return (pieces[1].strip(), pieces[2].strip()) if len(pieces) == 3 else ("", "")


def kept_parts(text: str) -> List[str]:
    """The parts a kept deliverable goal holds (after its overview), in order; [] for none."""
    body = FOLLOW_SPLIT.split(str(text or ""), maxsplit=1)[0]
    pieces = PART_SPLIT.split(body)
    return [p.strip() for p in pieces[1:] if p.strip()]


def with_reading_fields(metric: Mapping[str, Any]) -> Dict[str, Any]:
    """A metric record with the structured fields Metric Upkeep reads, taken from its own dated reading line when it has one (``Current value: <v> (as of
    <day>, from <source>)``): ``value`` (a number when it is one) and ``measured_through`` (that day, unless the record states one)."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    out = dict(metric)
    line = str(metric.get("record") or "").strip().split("\n")[0].strip()
    found = milestone_cue.Resolved.KEPT_READING.match(line)
    if found:
        raw = found.group("value").strip()
        try:
            out.setdefault("value", float(raw.replace(",", "")) if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", raw) else raw)
        except ValueError:
            out.setdefault("value", raw)
        if not str(out.get("measured_through") or "").strip():
            out["measured_through"] = found.group("day").strip()[:10]
    return out


def whole_row(registry: Any, cue: Any) -> Mapping[str, Any]:
    """The milestone's Solution record with its columns whole (the turn's read cuts long values, and the kept rubric line is read back whole); the row the
    cue holds when the lane cannot read it whole. The same read returns the milestone's metric records whole, and the cue's ``records_text`` (what the writer,
    the claim check and the reader of the turn's reads see) is rebuilt from them, so a fact past the cut is a fact the turn read."""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue
    if registry is None or registry.get("read_spec_records") is None:
        return cue.row
    try:
        result = registry.invoke("read_spec_records", {"quest_id": cue.quest_id, "whole_record_ids": [cue.rid, *cue.metric_ids]}, ToolContext(quest_id=cue.quest_id))
    except Exception:  # noqa: BLE001  the cue's own row stands
        return cue.row
    data = getattr(result, "data", None) or {}
    if not getattr(result, "ok", False):
        return cue.row
    records = data.get("records") or {}
    row = (records.get("solutions") or {}).get(cue.rid) or cue.row
    if records.get("metrics") and isinstance(cue.tables, Mapping):
        tables = {k: dict(v) for k, v in cue.tables.items()}
        tables["metrics"] = dict(tables.get("metrics") or {}, **{mid: m for mid, m in (records.get("metrics") or {}).items() if mid in cue.metric_ids})
        tables["solutions"] = dict(tables.get("solutions") or {}, **{cue.rid: row})
        cue.tables = tables
        cue.records_text = milestone_cue.records_read(tables, cue.rid, row)
    return row


def render_questions(questions: Sequence[Mapping[str, Any]]) -> str:
    """The evidence as kept text: each question, then its answer and source, or that it is still to be decided."""
    lines = []
    for n, q in enumerate(questions, 1):
        lines.append(f"Q{n}. {q['question']}")
        lines.append(f"A{n}. {q['answer']} (source: {q['source']})" if q.get("answer") else f"A{n}. {TO_BE_DECIDED}")
    return "\n".join(lines)


SOURCE_LABEL = re.compile(r"\(source: [^)]*\)")


QUESTION_LINE = re.compile(r"^Q(?P<n>\d+)\. (?P<q>.+)$")


ANSWER_LINE = re.compile(r"^A(?P<n>\d+)\. (?P<a>.+?)(?: \(source: (?P<s>[^)]*)\))?$")


def questions_of(text: str) -> Optional[Dict[str, Any]]:
    """The questions a kept evidence part holds (the shape ``render_questions`` writes), so the before is measured on what is kept; None when it holds none."""
    found: Dict[str, Dict[str, str]] = {}
    for line in str(text or "").splitlines():
        q, a = QUESTION_LINE.match(line.strip()), ANSWER_LINE.match(line.strip())
        if q:
            found.setdefault(q.group("n"), {})["question"] = q.group("q")
        elif a:
            answer = SOURCE_LABEL.sub("", a.group("a")).strip(" .;:,")
            answered = answer and answer != TO_BE_DECIDED and a.group("s")
            found.setdefault(a.group("n"), {}).update(answer=a.group("a").strip() if answered else "", source=(a.group("s") or "").strip() if answered else "")
    questions = [v for _, v in sorted(found.items(), key=lambda kv: int(kv[0])) if v.get("question")]
    return {"questions": questions} if questions else None


RECORD_ID = re.compile(r"\s*\((?:[a-z]{2,8}_[0-9a-f]{8,})\)|\b[a-z]{2,8}_[0-9a-f]{10,}\b|\s*\b(?:under |in |on )?(?:goal|task|record|id) [0-9a-f]{8,}\b")


def without_ids(text: str) -> str:
    """The text without internal record ids (``rep_4e8c0da18324``, ``esr_...``): a reader sees names, never ids."""
    return " ".join(RECORD_ID.sub("", str(text or "")).split())


def clip_line(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


WRITER_TIER_ENV = "QAR_MILESTONE_WRITER_TIER"


def writer_tier(cfg: Any = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """The tier a milestone deliverable is written on: ``QAR_MILESTONE_WRITER_TIER`` when set (a measured arm can be run this way), else the
    configuration's ``milestone_writer_tier``, else ``fast``. The structured calls (work items, checks, scoring) stay on the cheap tier whatever it is."""
    import os
    environ = os.environ if environ is None else environ
    return str(environ.get(WRITER_TIER_ENV) or getattr(cfg, "milestone_writer_tier", "") or "fast").strip()


WORK_SKILL_ENV = "QAR_DEEP_WORK_SKILL"


def work_skill(cfg: Any = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """The skill the deep worker carries out on a milestone deliverable's solution, by name: ``QAR_DEEP_WORK_SKILL`` when set, else the configuration's
    ``deep_work_skill``, else none (the plain brief)."""
    import os
    environ = os.environ if environ is None else environ
    return str(environ.get(WORK_SKILL_ENV) or getattr(cfg, "deep_work_skill", "") or "").strip()
