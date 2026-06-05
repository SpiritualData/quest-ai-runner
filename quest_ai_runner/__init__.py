"""quest-ai-runner — the orchestrator BRAIN (core) + the queued-task EXECUTOR (runner).

Generic, no consumer-specific logic. Three consumers:
  * Quest's own backend / a cockpit import ``quest_ai_runner.core`` IN-PROCESS for chat.
  * Integrating orgs / a personal lane run ``quest_ai_runner.runner`` (the Poller).
Consumers supply everything specific via ``quest_ai_runner.config.RunnerConfig``.
"""
from . import adapters, config, core, runner

__version__ = "0.1.0"
__all__ = ["core", "adapters", "runner", "config", "__version__"]
