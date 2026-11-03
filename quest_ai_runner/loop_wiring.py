"""What every part that runs the orchestrator's loop shares: the answer a turn returns, a context assembler that holds nothing, and
the one place a ``RunnerConfig`` over a model becomes an orchestrator.

``goal_agent`` (the whole runner) and the area modules (``area_reason_and_answer``, ``area_remember_and_recall``,
``area_take_in_the_world``) build the loop through ``orchestrator_over``, so no part writes its own wiring of the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from hubofhubs.ports.context import AssembledContext

from .config import RunnerConfig, build_orchestrator


@dataclass(frozen=True)
class GoalAnswer:
    """What a turn of the runner returned: the reply text, how the loop ended and what ran."""
    text: str
    kind: str                         # "answer", "deep", "confirm" or "cancelled"
    exit_reason: str = ""
    ran: Tuple[str, ...] = field(default_factory=tuple)    # the goals handed to a deep worker, in order
    question: str = ""                # what the runner asks the person, when kind is "confirm"
    steps: int = 0


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
    """The orchestrator of ``core/orchestrator.py`` over ``model``, built the way ``QuestAiRunnerLibrary`` builds it, with ``memory`` as
    its context assembler (none: ``NoContext``). ``fields`` set any other ``RunnerConfig`` field, for example ``deep_runner=None``
    to plan without ever running a worker."""
    return build_orchestrator(RunnerConfig(model_provider=model, context_assembler=memory if memory is not None else NoContext(), **fields))
