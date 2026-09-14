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
__all__ = ["core", "adapters", "runner", "config", "resources",
           "load_config", "load_client", "__version__"]


def load_client(config_path: Optional[str] = None, *, timeout: float = 30.0):
    """A ``QuestClient`` for this deployment's Quest account — the front door for API scripting.

    THE way to talk to the Quest API from anything that is not a full lane: a script, a notebook,
    an agent's one-off lookup, a cron job. No per-deployment client module, no ``sys.path``
    surgery, no re-deriving which environment variable holds the key::

        from quest_ai_runner import load_client

        client = load_client("~/my-lane/qar.toml")      # or set QAR_CONFIG_FILE
        client.whoami()
        client.add_quest_note(quest_id, "findings ...")
        client.create_decision("Approve the $250 order", assignee="owner",
                               default_on_silence="hold")

    ``config_path`` (or ``QAR_CONFIG_FILE`` when omitted) names the same TOML file a lane uses, so
    a lane and the scripts around it share one source of truth; omit both to build from
    ``QUEST_BASE_URL`` / ``QUEST_API_KEY`` / ``QUEST_TEAM_ID`` alone. The file's ``env_files`` /
    ``env_aliases`` tables are applied first, which is how a key kept in a chmod-600 ``.env`` under
    a deployment's own variable names reaches the client without being copied anywhere.

    Unlike :func:`load_config` this builds no adapter stack (no model provider, no vector store),
    so it is fast and needs none of the optional dependencies. Raises ``QuestNotConfigured`` if the
    base URL or key is missing — it never falls back to another account's credentials.

    Command line equivalent, for a lookup that needs no Python at all::

        quest-ai-runner quest whoami
        quest-ai-runner quest get_task task_123
    """
    from .config import load_quest_client

    return load_quest_client(config_path, timeout=timeout)


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
