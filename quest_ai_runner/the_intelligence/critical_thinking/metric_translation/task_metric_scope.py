"""Whose metric a task's result may show: the specification of the task's own quest, never the AI system's own records.

A task's result says what a metric was before and after the work. The metric is the one of the solution, in the task's own
quest or goal specification, that the work advanced. It is never a metric of the AI system itself (the records Quest AI Runner
ships with, or the hub library's): those measure the quality of the assistant, and a person reading a task about their own work
would take one for a measure of that work. The one exception is a task whose quest specification itself holds that solution
(work on the AI system, tracked in a quest that has its records).

This module is the guard and the scope, in code, so the rule does not rest on a prompt. It knows three things:

  * ``system_record_ids``   the ids of the AI system's own solutions and metrics (the record files Quest AI Runner, the esoi
                            library and the hub library ship, and Quest AI Runner's own ``esoi/`` folder when present).
  * ``refusal``             why a result (a tool call or a payload entry) is refused, or ``""`` when it may stand: it names a
                            system id that the task's quest specification does not hold.
  * ``scope_entries``       the entries of a finished goal's ``metrics`` payload with the refused ones taken out and the kept ones
                            carrying the quest and the solution's name, so a screen can open the solution's details.

Never raises into a run: an unreadable specification refuses a system id (it cannot be shown to belong) and keeps everything else.
No model call is made here.
"""
from __future__ import annotations

import csv
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("quest-ai-runner.task-metric-scope")

RECORD_FILES = {"solutions.csv": "solution_id", "metrics.csv": "metric_id"}
SOURCE_NOT_APPLICABLE = "not_applicable"
BLOCK_ENTRIES = 12
NAME_CHARS = 160

REFUSAL = ("Not recorded: {ids} belongs to the AI system's own records, not to this task's quest. Record the metric of a "
           "solution in THIS quest's specification (its solutions are listed in your brief); if none fits, add or extend one "
           "there with apply_spec_edit and a metric linked to it, then record that. not_applicable with a one-sentence reason "
           "when no solution of the quest covers the work.")


def library_folders() -> List[Path]:
    """The folders whose record files hold the AI system's own solutions and metrics: this package, the esoi library, the hub
    library, and the ``esoi/`` folder at the root of the Quest AI Runner checkout when there is one."""
    here = Path(__file__).resolve()
    folders: List[Path] = [p for p in here.parents if p.name == "quest_ai_runner"][:1]
    if folders:
        root_esoi = folders[0].parent / "esoi"
        if root_esoi.is_dir():
            folders.append(root_esoi)
    for module in ("esoi", "hubofhubs"):
        try:
            imported = __import__(module)
            folders.append(Path(imported.__file__).resolve().parent)
        except Exception:  # noqa: BLE001  a library that is not installed has no records to name
            continue
    return folders


@lru_cache(maxsize=1)
def system_record_ids() -> frozenset:
    """Every solution id and metric id in the AI system's own record files. Read once per process."""
    found = set()
    for folder in library_folders():
        for name, column in RECORD_FILES.items():
            for path in folder.rglob(name):
                try:
                    with path.open(newline="", encoding="utf-8") as handle:
                        for row in csv.DictReader(handle):
                            value = str(row.get(column) or "").strip()
                            if value:
                                found.add(value)
                except Exception:  # noqa: BLE001  one unreadable file does not decide the rest
                    log.debug("record file not read: %s", path, exc_info=True)
    return frozenset(found)


def read_quest_spec(env: Mapping[str, str], quest_id: Optional[str]) -> Optional[Mapping[str, Mapping[str, Mapping[str, Any]]]]:
    """``{table: {record id: fields}}`` of the quest's specification, or None when it cannot be read (no quest, no credentials,
    Quest not answering). None is not "empty": a caller treats it as "cannot show a system id belongs here"."""
    if not quest_id:
        return None
    try:
        from quest_ai_runner.the_body.internal_context.quest.spec_records import QuestSpecRecords
        from quest_ai_runner.the_world.tools.tools import quest_client_from_env, quest_credentials_present
        if not quest_credentials_present(env):
            return None
        return QuestSpecRecords(quest_client_from_env(env)).read(quest_id)
    except Exception:  # noqa: BLE001
        log.debug("quest specification not read", exc_info=True)
        return None


def held_ids(spec: Optional[Mapping[str, Mapping[str, Any]]]) -> set:
    """The solution and metric ids the quest's specification itself holds."""
    if not spec:
        return set()
    return set((spec.get("solutions") or {}).keys()) | set((spec.get("metrics") or {}).keys())


def foreign_ids(solution_id: str, metric_id: str, spec: Optional[Mapping[str, Mapping[str, Any]]]) -> List[str]:
    """The ids among the two that are the AI system's own and that the task's quest specification does not hold."""
    held = held_ids(spec)
    system = system_record_ids()
    return [i for i in (metric_id, solution_id) if i and i in system and i not in held]


def refusal(solution_id: str, metric_id: str, spec: Optional[Mapping[str, Mapping[str, Any]]]) -> str:
    """The sentence a worker is told when its result names the AI system's own record, or ``""`` when the result may stand."""
    bad = foreign_ids(str(solution_id or ""), str(metric_id or ""), spec)
    return REFUSAL.format(ids=", ".join(bad)) if bad else ""


def solution_of_metric(metric_id: str, spec: Optional[Mapping[str, Mapping[str, Any]]]) -> str:
    """The id of the solution a metric of the quest's specification measures (the metric's first link), or ``""``."""
    row = ((spec or {}).get("metrics") or {}).get(metric_id) or {}
    linked = row.get("solution_ids") or []
    return str(linked[0]) if linked else ""


def scope_entries(entries: Sequence[Mapping[str, Any]], quest_id: Optional[str],
                  spec: Optional[Mapping[str, Mapping[str, Any]]]) -> Tuple[List[Dict[str, Any]], int]:
    """``(entries to keep, how many were refused)`` for a goal's ``metrics`` payload.

    An entry naming an id of the AI system's own records that the quest's specification does not hold is taken out (a declared
    not-applicable entry keeps its reason and loses only the foreign solution id). A kept entry that points at a solution of the
    quest's specification (by its solution id, else by its metric's link) carries ``quest_id`` and ``solution_name``, so a screen
    can load the solution's details from the quest's specification."""
    kept: List[Dict[str, Any]] = []
    refused = 0
    solutions = (spec or {}).get("solutions") or {}
    for raw in entries or []:
        entry = dict(raw)
        solution_id, metric_id = str(entry.get("solution_id") or ""), str(entry.get("metric_id") or "")
        if foreign_ids(solution_id, metric_id, spec):
            if entry.get("source") == SOURCE_NOT_APPLICABLE and not foreign_ids("", metric_id, spec):
                entry["solution_id"] = ""
            else:
                refused += 1
                continue
        solution_id = str(entry.get("solution_id") or "") or solution_of_metric(metric_id, spec)
        if solution_id and solution_id in solutions and quest_id:
            entry["solution_id"] = solution_id
            entry["quest_id"] = str(quest_id)
            entry["solution_name"] = str(solutions[solution_id].get("name") or "").strip()[:NAME_CHARS]
        kept.append(entry)
    return kept, refused


def quest_solutions_block(spec: Optional[Mapping[str, Mapping[str, Any]]], quest_id: Optional[str]) -> str:
    """The brief's block naming the solutions of THIS task's quest and the metric of each, written by code from the quest's
    records, so a worker records against these and never searches for one. ``""`` when there is nothing to name."""
    solutions = (spec or {}).get("solutions") or {}
    if not solutions:
        return ""
    metrics = (spec or {}).get("metrics") or {}
    lines = []
    for sid, row in list(solutions.items())[:BLOCK_ENTRIES]:
        authoritative = str(row.get("authoritative_metric_id") or "")
        linked = [m for m, r in metrics.items() if sid in (r.get("solution_ids") or [])]
        metric_id = authoritative if authoritative in metrics else (linked[0] if len(linked) == 1 else "")
        metric = f"metric {metric_id} ({str((metrics.get(metric_id) or {}).get('name') or '').strip()})" if metric_id else "no metric yet"
        lines.append(f"- solution {sid}: {str(row.get('name') or '').strip()}; {metric}")
    return ("\n\nTHIS TASK'S QUEST SPECIFICATION" + (f" ({quest_id})" if quest_id else "") + ". A metric result is recorded "
            "against one of these solutions and its metric, never against a record of the AI system itself:\n" + "\n".join(lines))


class QuestScopedSource:
    """A slice source that gives no slice for a solution or metric of the AI system's own records the task's quest does not hold.

    It wraps the source a goal reads the repository's specification slice from (the working folder's records), so the brief's NEXT
    PRIORITY block and the ``measured`` rows never point a task at the AI system's own metric. Everything else passes through."""

    def __init__(self, inner: Any, spec_of: Any):
        self.inner, self.spec_of = inner, spec_of

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def slice_for(self, request: str, working_dir: Optional[str] = None) -> Any:
        found = self.inner.slice_for(request, working_dir)
        if found is None:
            return None
        if foreign_ids(str(getattr(found, "solution_id", "") or ""), str(getattr(found, "metric_id", "") or ""), self.spec_of()):
            return None
        return found


def own_ids_in(values: Iterable[str]) -> List[str]:
    """The ids among ``values`` that are the AI system's own (for a test or a log line)."""
    system = system_record_ids()
    return [v for v in values if v in system]
