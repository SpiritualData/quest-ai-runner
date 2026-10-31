"""Staged plans: a thin binding over the hub's ``hubofhubs.orchestrate.staged_plan``.

The planner's ``deep_subtasks`` may nest (a list is a sequence, a step may be a list of tasks that run together, a task may say
``run_when``). The normalizer, the stage split and ``step_allows`` are the hub's; QAR runs a nested plan in process by default
(``Orchestrator._run_deep``), and a deep runner that satisfies the ``StagedPlanRunner`` port (``core/adapters_deep.py``)
receives each sequence's stages. Off unless ``OrchestratorConfig.staged_plans`` is set.
"""
from hubofhubs.orchestrate.staged_plan import (  # noqa: F401
    RUN_WHEN_VALUES, count_tasks, is_staged, normalize_staged_subtasks, sequence_stages, split_plan, step_allows,
    tasks_flat, tasks_in,
)
