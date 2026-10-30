"""Staged plans: the planner's ``deep_subtasks`` as an ordered list of stages.

The planner may write ``deep_subtasks`` as before (a flat list of task objects, all run at once) or
with nesting, a list of lists:

  * an object is one task;
  * a list is a SEQUENCE: its entries run one after another, and an entry that is itself a list of
    objects is a step whose tasks run together (so ``[[A, [B1, B2], C]]`` is A, then B1 and B2
    together, then C);
  * the top level runs concurrently, as it always has.

A task object is ``{"goal", "brief"}`` plus an optional ``"run_when"``: ``"always"`` (the default),
``"prev_met"`` (only if the step before it met its goal) or ``"prev_not_met"`` (only if it did not),
which is how "do A, then B, and if B's goal was not met create another task" is written.

QAR runs a nested plan in process by default (``Orchestrator._run_deep``). A consumer whose deep
runner can realise it differently, for example as queued tasks with a real wait between stages,
offers ``run_staged_plan`` (see ``DeepRunnerBase``) and receives ``plan_stages`` of it. This module
knows nothing about any consumer.

Off unless ``OrchestratorConfig.staged_plans`` is set: the planner schema and the normalizer then
accept only the flat list, exactly as before.
"""
from __future__ import annotations

from typing import Any, Dict, List

RUN_WHEN_VALUES = ("always", "prev_met", "prev_not_met")


def _clean_task(raw: Any) -> Dict[str, Any] | None:
    if not isinstance(raw, dict) or not (raw.get("goal") or raw.get("brief")):
        return None
    run_when = raw.get("run_when")
    return {"goal": raw.get("goal") or None, "brief": raw.get("brief") or None,
            "run_when": run_when if run_when in RUN_WHEN_VALUES else "always"}


def _clean_step(step: Any) -> Any:
    """A sequence entry: one task, or a list of tasks that run together. None when empty."""
    if isinstance(step, list):
        tasks = [t for t in (_clean_task(x) for x in step) if t]
        return tasks or None
    return _clean_task(step)


def normalize_staged_subtasks(raw: Any, *, max_items: int, max_stages: int, max_tasks: int) -> List[Any]:
    """The planner's ``deep_subtasks`` with nesting allowed, cleaned and capped.

    ``max_items`` caps the top level (the same cap a flat list has always had), ``max_stages`` how
    many steps one sequence may have, ``max_tasks`` the tasks across the whole plan. A list holding a
    single task is just that task. Anything that is not a task or a list of tasks is dropped.
    """
    items: List[Any] = []
    budget = max_tasks
    for entry in (raw or []):
        if budget <= 0 or len(items) >= max_items:
            break
        if isinstance(entry, list):
            steps: List[Any] = []
            for step in entry[:max_stages]:
                cleaned = _clean_step(step)
                if cleaned is None:
                    continue
                size = len(cleaned) if isinstance(cleaned, list) else 1
                if size > budget:
                    if isinstance(cleaned, list):
                        cleaned = cleaned[:budget]
                        size = len(cleaned)
                    else:
                        break
                budget -= size
                steps.append(cleaned)
            if len(steps) == 1:
                items.append(steps[0] if isinstance(steps[0], dict) else steps)
            elif steps:
                items.append(steps)
        else:
            task = _clean_task(entry)
            if task:
                items.append(task)
                budget -= 1
    return items


def tasks_in(item: Any) -> List[Dict[str, Any]]:
    """Every task object inside one top-level entry, in order."""
    if isinstance(item, dict):
        return [item]
    found: List[Dict[str, Any]] = []
    for part in (item if isinstance(item, list) else []):
        found.extend(tasks_in(part))
    return found


def count_tasks(subtasks: List[Any]) -> int:
    return sum(len(tasks_in(item)) for item in (subtasks or []))


def is_staged(subtasks: List[Any]) -> bool:
    """Whether the plan has an ORDER in it: a sequence of two or more steps, or any non-default ``run_when``."""
    for item in (subtasks or []):
        if isinstance(item, list) and len(item) > 1:
            return True
        if any(t.get("run_when", "always") != "always" for t in tasks_in(item)):
            return True
    return False


def plan_stages(subtasks: List[Any]) -> List[List[Dict[str, Any]]]:
    """The plan as ordered stages, each a list of tasks that may run in parallel.

    Stage k holds every top-level task (stage 0) and the k-th step of every sequence.
    """
    stages: List[List[Dict[str, Any]]] = []

    def put(index: int, tasks: List[Dict[str, Any]]) -> None:
        while len(stages) <= index:
            stages.append([])
        stages[index].extend(tasks)

    for item in (subtasks or []):
        if isinstance(item, dict):
            put(0, [item])
        elif isinstance(item, list):
            for index, step in enumerate(item):
                put(index, tasks_in(step))
    return [stage for stage in stages if stage]
