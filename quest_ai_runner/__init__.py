"""quest-ai-runner — the orchestrator BRAIN (core) + the queued-task EXECUTOR (runner).

Generic, no consumer-specific logic. Three consumers:
  * Quest's own backend / a cockpit import ``quest_ai_runner.core`` IN-PROCESS for chat.
  * Integrating orgs / a personal lane run ``quest_ai_runner.runner`` (the Poller).
Consumers supply everything specific via ``quest_ai_runner.config.RunnerConfig``.
"""
from typing import TYPE_CHECKING, Optional

from . import adapters, config, core, resources, runner

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import RunnerConfig

__version__ = "0.1.0"
__all__ = ["core", "adapters", "runner", "config", "resources", "load_config", "__version__"]


def load_config(config_path: Optional[str] = None) -> "RunnerConfig":
    """Build a ``RunnerConfig`` from a TOML file layered under the environment.

    THE front door for a consumer: a lane is ``load_config()`` plus
    ``quest_ai_runner.runner.lane.run_lane`` (see ``docs/tutorial-your-first-lane.md``). Every
    field a file can set is listed in ``docs/writing-a-consumer.md``; an environment variable
    always wins over the same field in the file.

    ``config_path`` (or ``QAR_CONFIG_FILE`` when omitted) names the file; omit both to build from
    the environment alone. A bad file raises ``config.ConfigFileError`` at startup rather than
    degrading silently.

    This is the public, supported name. The implementation lives in ``cli._config_from_env``
    because it also builds the CLI's own adapter stack; that private name stays as an alias for
    in-repo callers, but nothing outside this package should import it.
    """
    from .cli import _config_from_env  # local: cli imports config, so this cannot be top-level

    return _config_from_env(config_path)
