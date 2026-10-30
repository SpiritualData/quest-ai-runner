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

QAR runs a nested plan in process by default (``Orchestrator._run_deep``), honouring ``run_when``
(a task whose condition is not met is not run). A consumer whose deep runner can realise it
differently, for example as queued tasks with a real wait between stages, satisfies the
``StagedPlanRunner`` port (``core/adapters_deep.py``, checked with ``isinstance``) and receives
``sequence_stages`` of EACH top-level sequence: one plan per sequence, because independent sequences
must never share a stage. This module knows nothing about any consumer.

Off unless ``OrchestratorConfig.staged_plans`` is set: the planner schema and the normalizer then
accept only the flat list, exactly as before.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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


def _first_step_always(step: Any) -> Any:
    """A task with nothing before it has nothing to be conditional on: its ``run_when`` is ``always``."""
    if isinstance(step, list):
        return [dict(t, run_when="always") for t in step]
    return dict(step, run_when="always")


def normalize_staged_subtasks(raw: Any, *, max_items: int, max_stages: int, max_tasks: int) -> List[Any]:
    """The planner's ``deep_subtasks`` with nesting allowed, cleaned and capped.

    ``max_items`` caps the top level (the same cap a flat list has always had), ``max_stages`` how
    many steps one sequence may have, ``max_tasks`` the tasks across the whole plan. A list holding a
    single task is just that task. Anything that is not a task or a list of tasks is dropped. The
    first step of a sequence and every top-level task are ``always`` (nothing comes before them).
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
            if steps:
                steps[0] = _first_step_always(steps[0])
            if len(steps) == 1:
                items.append(steps[0] if isinstance(steps[0], dict) else steps)
            elif steps:
                items.append(steps)
        else:
            task = _clean_task(entry)
            if task:
                items.append(dict(task, run_when="always"))
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


def sequence_stages(sequence: List[Any]) -> List[List[Dict[str, Any]]]:
    """ONE sequence as ordered stages: stage k holds the tasks of its k-th step.

    Each top-level sequence is its own plan. Merging the stages of independent sequences would make
    chain A's second step wait on chain B and judge its ``run_when`` on both.
    """
    stages = [tasks_in(step) for step in (sequence or [])]
    return [stage for stage in stages if stage]


def split_plan(subtasks: List[Any]) -> Tuple[List[Dict[str, Any]], List[List[List[Dict[str, Any]]]]]:
    """``(flat_tasks, plans)``: the top-level tasks that stand alone, and one stage list per sequence."""
    flat: List[Dict[str, Any]] = []
    plans: List[List[List[Dict[str, Any]]]] = []
    for item in (subtasks or []):
        if isinstance(item, dict):
            flat.append(item)
        elif isinstance(item, list):
            stages = sequence_stages(item)
            if stages:
                plans.append(stages)
    return flat, plans


def tasks_flat(subtasks: List[Any]) -> List[Dict[str, Any]]:
    """Every task object of a plan in order, whatever the nesting (for goals and cancel paths)."""
    found: List[Dict[str, Any]] = []
    for item in (subtasks or []):
        found.extend(tasks_in(item))
    return found


def step_allows(run_when: Optional[str], previous_results: Optional[List[Any]]) -> bool:
    """Whether a task whose condition is ``run_when`` runs, given the results of the step before it.

    ``previous_results`` is None for the first step (nothing before it: ``always`` only matters).
    A step is met when every result in it is met. Same meaning as the consumer-side stages.
    """
    if previous_results is None or run_when in (None, "always"):
        return True
    met = bool(previous_results) and all(getattr(r, "met", False) for r in previous_results)
    if run_when == "prev_met":
        return met
    if run_when == "prev_not_met":
        return not met
    return True
