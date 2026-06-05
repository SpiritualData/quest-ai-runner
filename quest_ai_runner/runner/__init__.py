"""quest_ai_runner.runner — the EXECUTOR (the watchdog generalized).

Consumers that bring an external execution lane run this: a Poller discovers due queued Quest
tasks, claims them, runs each through ``core.Orchestrator`` via a TaskExecutor, and reports the
result back over the QuestClient (done / needs_you+decision / failed).
"""
from .executor import ExecutionOutcome, TaskExecutor
from .poller import Poller, StateStore
from .quest_client import (
    QuestApiError,
    QuestClient,
    QuestDecisionSink,
    QuestNotConfigured,
)

__all__ = [
    "Poller", "StateStore", "TaskExecutor", "ExecutionOutcome",
    "QuestClient", "QuestDecisionSink", "QuestNotConfigured", "QuestApiError",
]
