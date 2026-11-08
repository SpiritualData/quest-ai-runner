"""quest-ai-runner — the orchestrator BRAIN (core) + the queued-task EXECUTOR (runner).

Generic, no consumer-specific logic. Three consumers:
  * Quest's own backend / a cockpit import ``quest_ai_runner.core`` IN-PROCESS for chat.
  * Integrating orgs / a personal lane run ``quest_ai_runner.runner`` (the Poller).
Consumers supply everything specific via ``quest_ai_runner.the_body.configuration.config.RunnerConfig``.
"""
from typing import TYPE_CHECKING, Optional

from quest_ai_runner.the_self.assembler.hub_path import ensure_hub

ensure_hub()   # before anything imports hubofhubs: an environment built before it was required still starts

from quest_ai_runner.the_body.configuration import config
from quest_ai_runner.the_body.host import resources  # noqa: E402
from quest_ai_runner.the_intelligence.mind.delegation import goal_runner
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser import model_registry
from quest_ai_runner.the_world.tools import tools as core_tools  # noqa: E402
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import cli_model as _cli_model
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import quest_client_from_env as _quest_client_from_env

# ``core`` may not import ``adapters`` or ``runner``, so the one place that sees all of them wires in
# what core needs: the Claude Code model translator for the deep worker's model gate
# (``core.goal_runner.cli_safe_model``) and the Quest client factory for the standard tools.
goal_runner.set_cli_model_translator(_cli_model)
core_tools.set_quest_client_factory(_quest_client_from_env)
# the work trace judges whether a reply draws on a record by words rare in general English, a table that lives with the context adapters.
from quest_ai_runner.the_intelligence.subconscious.context_retriever.personal_lexicon import word_idf as _word_idf, MAX_IDF as _MAX_IDF  # noqa: E402
from quest_ai_runner.the_intelligence.mind.response_writer import work_trace as _work_trace  # noqa: E402
_work_trace.set_word_rarity(lambda word: _word_idf(word) >= _MAX_IDF)
# core reads no environment: the prepaid credit the tier selector counts comes from the environment through providers.py.
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.providers import credits_from_environment as _credits_from_environment  # noqa: E402

model_registry.set_credit_source(_credits_from_environment)
# core reads no environment variable (S13): its tunables arrive as ``core.runtime_settings`` values,
# resolved by the config layer from the live environment unless a RunnerConfig pins its own.
from quest_ai_runner.the_body.configuration.config_core_settings import install_environment_backed_core as _install_core_env  # noqa: E402

_install_core_env()

# QAR is Quest's connector to the hub of hubs (``hubofhubs``, a required dependency): the one startup registration of the
# parts QAR owns, under names QAR owns, where the hub has a seat for them (quest_ai_runner/the_self/assembler/hub.py).
from quest_ai_runner.the_self.assembler import hub as _hub  # noqa: E402
from quest_ai_runner.the_self.assembler.hub import register_with_hub as _register_with_hub  # noqa: E402

_register_with_hub()

if TYPE_CHECKING:  # pragma: no cover — typing only
    from quest_ai_runner.the_body.configuration.config import RunnerConfig

__version__ = "0.5.1"
__all__ = ["config", "resources",
           "load_config", "load_client", "assembler", "run", "__version__"]


WHOLE_RUNNER = {"quest-ai-runner": "quest-self"}   # the package's own name reaches the solution that is the whole runner


def run(target, input=None, **overrides):
    """One line for any solution or intent QAR knows (its own and every hub seat it builds on): ``quest_ai_runner.run(id,
    input)`` returns what that solution's interface returns (``esoi.entry``). Every seat comes from QAR's selections, then
    the hub's; keywords override only what differs; ``environ`` defaults to the process environment."""
    from esoi import run_entry as entry_run
    return entry_run(WHOLE_RUNNER.get(target, target), input, assembler=_hub.child(), **overrides)


run.assembler = None   # bound lazily: _hub.child() builds QAR's Assembler on first use


def assembler():
    """QAR's hub Assembler (scope ``quest_ai_runner``), the entry point ``esoi requirements --package quest_ai_runner`` and
    ``esoi lock --package quest_ai_runner`` look for."""
    return _hub.child()


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
    from quest_ai_runner.the_body.configuration.config import load_quest_client

    return load_quest_client(config_path, timeout=timeout)


def load_config(config_path: Optional[str] = None) -> "RunnerConfig":
    """Build a ``RunnerConfig`` from a TOML file layered under the environment.

    THE front door for a consumer: a lane is ``load_config()`` plus
    ``quest_ai_runner.the_consciousness.request_intake.lane.run_lane`` (see ``docs/tutorials/tutorial-your-first-lane.md``). Every
    field a file can set is listed in ``docs/how-to/writing-a-consumer.md``; an environment variable
    always wins over the same field in the file.

    ``config_path`` (or ``QAR_CONFIG_FILE`` when omitted) names the file; omit both to build from
    the environment alone. A bad file raises ``config.ConfigFileError`` at startup rather than
    degrading silently.

    This is the public, supported name. The implementation lives in ``cli._config_from_env``
    because it also builds the CLI's own adapter stack; that private name stays as an alias for
    in-repo callers, but nothing outside this package should import it.
    """
    from quest_ai_runner.the_consciousness.terminal.cli import _config_from_env  # local: cli imports config, so this cannot be top-level

    return _config_from_env(config_path)
