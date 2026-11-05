"""What every part that runs the orchestrator's loop shares: the answer a turn returns, a context assembler that holds nothing, and
the one place a ``RunnerConfig`` over a model becomes an orchestrator.

``respond`` (the whole runner) and the area modules (``area_mind``, ``area_take_in_the_world``) build the loop through ``orchestrator_over``, so no part writes its own wiring of the orchestrator.
"""
from __future__ import annotations

from typing import Any, Optional

from hubofhubs.api import AssembledContext

from quest_ai_runner.the_body.configuration.config import RunnerConfig, build_orchestrator
from quest_ai_runner.the_self.interface import GoalAnswer



def goal_answer(result: Any) -> GoalAnswer:
    """The answer a caller reads, from what ``Orchestrator.run`` returned."""
    return GoalAnswer(text=result.text or result.question or "", kind=result.kind, exit_reason=result.exit_reason,
                      ran=tuple(result.goals), question=result.question or "", steps=result.steps)


class NoContext:
    """A context assembler that holds nothing: the loop over it reads no cards and writes none, so a part that only needs the loop
    (routing a message, writing the planner's prompt) touches no store."""

    def assemble(self, task_text: str, *, meta: Optional[dict] = None, on_event: Optional[Any] = None) -> AssembledContext:
        return AssembledContext()

    def record(self, task_text: str, outcome: dict) -> None:
        return None


def orchestrator_over(model: Any, memory: Any = None, **fields: Any) -> Any:
    """The orchestrator of ``core/orchestrator.py`` over ``model``, built the way ``Respond`` builds it, with ``memory`` as
    its context assembler (none: ``NoContext``). ``fields`` set any other ``RunnerConfig`` field, for example ``deep_runner=None``
    to plan without ever running a worker."""
    return build_orchestrator(RunnerConfig(model_provider=model, context_assembler=memory if memory is not None else NoContext(), **fields))
