"""The slice of the specification a goal serves: the next priority and the metric that measures it, found without a model.

``repo_specs`` finds the ESOI records a request concerns. A deep goal needs less than that and says it more exactly: which
solution the work serves, the NEXT milestone of that solution (its priorities are in order, so the next one is the first that
is not archived), and the solution's authoritative metric with the command that records it. That is a ``SpecSlice``. It is
small enough to sit in a brief, and it is what the metric before and after is taken against (``metric_meter``).

Nothing here calls a model and nothing here guesses:

  * the records are the repository's own ``esoi/`` files, found from the working folder (``repo_specs.find_root``); outside
    a repository that has them, or with no working folder to look from, the answer is ``None``;
  * the solution is chosen by the same lexical selection ``find_specs`` uses, and only when it is clear: the selection is
    confident, or every candidate it offers belongs to one solution. An ambiguous request gets no slice, because a wrong
    metric measured with confidence is worse than none;
  * a milestone's target is a number only when its text holds exactly one number; otherwise it is ``None``. A percent is
    read as a fraction (``100%`` is 1.0), the scale of a pass rate or a share;
  * a metric is the solution's ``authoritative_metric_id``, or its only metric; a solution with several and no named one
    gets none.

A milestone is archived when its text begins with ``Archived`` (or ``(archived)`` or ``[archived]``): the ESOI records name
no other way, so this reader understands that one.

The lookup runs in a thread with a timeout (``SLICE_TIMEOUT_SECONDS``) and never raises. It uses the lexical search only: the
optional local embedder of ``repo_specs.find`` costs seconds the first time, which a brief should not wait for.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from . import repo_specs
from .runtime_settings import process_environment
from .spec_selection import select_specs

log = logging.getLogger("quest-ai-runner.spec-slice")

SLICE_TIMEOUT_SECONDS = 10.0
MILESTONE_CHARS = 280

NUMBERED_ITEM = re.compile(r"(?:(?<=\s)|^)(\d{1,2})\.\s+(?=\S)")
ARCHIVED_MARK = re.compile(r"^[\(\[]?\s*archived\b[\)\]]?\s*[:\-]?\s*", re.IGNORECASE)
NUMBER_TOKEN = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)(?![\w])")


@dataclass(frozen=True)
class Milestone:
    number: int
    text: str
    archived: bool = False


@dataclass(frozen=True)
class SpecSlice:
    """What a goal serves. ``root`` is where the records were found (never sent to a consumer: see ``to_dict``)."""
    root: str
    solution_id: str
    solution_name: str = ""
    milestone: str = ""                  # the next open milestone's text; "" when the solution has none
    milestone_number: int = 0
    target: Optional[float] = None       # the one number in the milestone, or None
    target_text: str = ""
    metric_id: str = ""
    metric_name: str = ""
    metric_command: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"solution_id": self.solution_id, "solution_name": self.solution_name, "milestone": self.milestone,
                "milestone_number": self.milestone_number, "target": self.target, "target_text": self.target_text,
                "metric_id": self.metric_id, "metric_name": self.metric_name}


class SliceSource(Protocol):
    """Where a goal's slice comes from. ``None`` means there is none to give (not a repository with records, or unclear)."""

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        ...


def parse_milestones(cell: str) -> List[Milestone]:
    """The ordered milestones of a solution's ``milestones`` cell: ``1. text. 2. text.``, or one unnumbered text.

    A numbered list is read only when the cell starts with ``1.`` and the numbers run 1, 2, 3 in order, so a number inside a
    sentence ("on 2026-10-19. 2 of them") never splits one."""
    text = " ".join((cell or "").split())
    if not text:
        return []
    marks = []
    expect = 1
    for found in NUMBERED_ITEM.finditer(text):
        if int(found.group(1)) == expect:
            marks.append(found)
            expect += 1
    if not marks or marks[0].start() != 0:
        return [milestone(1, text)]
    items = []
    for i, found in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        items.append(milestone(i + 1, text[found.end():stop]))
    return items


def milestone(number: int, text: str) -> Milestone:
    text = text.strip()
    archived = ARCHIVED_MARK.match(text)
    return Milestone(number, text[archived.end():].strip() if archived else text, bool(archived))


def next_milestone(cell: str) -> Optional[Milestone]:
    """The first milestone that is not archived, or None."""
    return next((m for m in parse_milestones(cell) if not m.archived and m.text), None)


def target_of(text: str) -> tuple:
    """``(value, token)`` when the text holds exactly one number, else ``(None, "")``. A percent is a fraction."""
    found = list(NUMBER_TOKEN.finditer(text or ""))
    if len(found) != 1:
        return None, ""
    value = float(found[0].group(1))
    return (value / 100 if found[0].group(2) else value), found[0].group(0)


def solutions_of(chosen: Any, index: Dict[str, Any]) -> List[str]:
    """The solution ids a selection settles on: the best one when the selection is sure, the only one otherwise.

    Candidates are the selected records themselves (not the ones brought in as links): a solution is itself, a metric or an
    implementation is the solution it belongs to; a problem or an ideal serves several solutions and names none."""
    by_id = {e["id"]: e for e in index["entries"]}
    found: List[str] = []
    for entry_id in chosen.ids:
        entry = by_id.get(entry_id)
        if entry is None or entry_id in chosen.linked:
            continue
        if entry["kind"] == "solution":
            owners = [entry_id]
        elif entry["kind"] in ("metric", "implementation"):
            owners = [ref for ref in entry["refs"] if by_id.get(ref, {}).get("kind") == "solution"]
        else:
            owners = []
        found.extend(o for o in owners if o not in found)
    return found[:1] if chosen.route == "sure" or len(found) == 1 else []


def authoritative_metric(solution: Dict[str, str], metrics: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    """The solution's named authoritative metric record, else its only metric, else None."""
    from esoi.tables import ids
    named = (solution.get("authoritative_metric_id") or "").strip()
    linked = ids(solution.get("metric_ids", ""))
    pick = named or (linked[0] if len(linked) == 1 else "")
    return metrics.get(pick)


def slice_from_repo(request: str, working_dir: Optional[str]) -> Optional[SpecSlice]:
    """The slice for a request, or None. Reads the records; never raises."""
    try:
        if not working_dir and not process_environment().get("QAR_SPEC_ROOT"):
            return None   # no folder to look from: the process's own would be a guess about where the work happens
        root = repo_specs.find_root(Path(working_dir) if working_dir else None)
        if root is None:
            return None
        from esoi import config as esoi_config, registry
        index = repo_specs.repo_index(root)
        wanted = solutions_of(select_specs(request, index, vector_search=None), index)
        if not wanted:
            return None
        loaded = registry.load(esoi_config.load(Path(root)))
        solution = next((r for r in loaded.every_solution() if r.get("solution_id") == wanted[0]), None)
        if solution is None:
            return None
        metric = authoritative_metric(solution, {m.get("metric_id", ""): m for m in loaded.metrics}) or {}
        step = next_milestone(solution.get("milestones", ""))
        if step is None and not metric:
            return None
        value, token = target_of(step.text if step else "")
        return SpecSlice(root=str(root), solution_id=wanted[0], solution_name=solution.get("name", ""),
                         milestone=step.text if step else "", milestone_number=step.number if step else 0,
                         target=value, target_text=token, metric_id=metric.get("metric_id", ""),
                         metric_name=metric.get("name", ""), metric_command=metric.get("command", ""))
    except Exception:  # noqa: BLE001  a missing or odd record set means no slice, never a failed run
        log.debug("no specification slice", exc_info=True)
        return None


def describe_metric(metric_id: str, working_dir: Optional[str] = None) -> Dict[str, str]:
    """``{"name", "solution_id"}`` of a metric record in the repository around ``working_dir``, ``{}`` when unknown."""
    try:
        root = repo_specs.find_root(Path(working_dir) if working_dir else None)
        if root is None or not metric_id:
            return {}
        from esoi import config as esoi_config, registry
        from esoi.tables import ids
        loaded = registry.load(esoi_config.load(Path(root)))
        record = next((m for m in loaded.metrics if m.get("metric_id") == metric_id), None)
        if record is None:
            return {}
        owners = [r["solution_id"] for r in loaded.every_solution() if metric_id in ids(r.get("metric_ids", ""))]
        return {"name": record.get("name", ""), "solution_id": owners[0] if len(owners) == 1 else ""}
    except Exception:  # noqa: BLE001
        log.debug("metric not described", exc_info=True)
        return {}


class RepoSliceSource:
    """The slice from the repository's ESOI records, found by lexical selection, with a time limit."""

    def __init__(self, timeout_seconds: float = SLICE_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Optional[SpecSlice]:
        box: Dict[str, Optional[SpecSlice]] = {}
        worker = threading.Thread(target=lambda: box.update(found=slice_from_repo(request, working_dir)),
                                  name="spec-slice", daemon=True)
        worker.start()
        worker.join(self.timeout_seconds)
        return box.get("found")


def next_priority_block(found: SpecSlice) -> str:
    """The short block a brief carries: the next milestone, the metric, and what to record."""
    metric = (f"{found.metric_name or found.metric_id} (metric_id {found.metric_id})" if found.metric_id
              else "none is named for this solution; record the one that applies")
    return "\n".join([
        f"NEXT PRIORITY for this work (solution_id {found.solution_id}):",
        "Milestone: " + (found.milestone[:MILESTONE_CHARS] if found.milestone else "none is set for this solution"),
        f"Metric: {metric}",
        "Record the metric's value before and after your change with record_metric_result; if no metric applies to "
        "this work, record not_applicable with a one-sentence reason."])
