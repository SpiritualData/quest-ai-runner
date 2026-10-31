"""Goal runner: QAR's binding of the hub's subprocess goal runner (``hubofhubs.deep.subprocess_runner``).

The "run to a written done-standard, bounded by --max-turns" contract and the reference runner that spawns Claude Code headless
are the hub's. ``SubprocessGoalRunner`` here is the hub's class with QAR's settings (``goal_runner_contract.goal_settings``: the
``QAR_*`` timeouts and launch flags, the escalation marker, the usage-limit state, the standing instructions, the prompt budget and
the requester environment) and the process-wide model translator the composition root registers. It stays the
``run-deep-work`` implementation named ``subprocess``.

This module also re-exports every name of ``goal_runner_contract`` and ``goal_runner_session`` under the old import path.
"""
from __future__ import annotations

from typing import Callable, Optional

from esoi import implementation

from hubofhubs.deep.launch import DeepLaunchSettings
from hubofhubs.deep.settings import GoalSettings
from hubofhubs.ports.deep import DeepResult, DeepRunner, LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED, ProgressEvent  # noqa: F401
from hubofhubs.ports.events import EVENT_EXEC  # noqa: F401
from hubofhubs.deep.subprocess_runner import SubprocessGoalRunner as HubSubprocessGoalRunner

from .goal_runner_contract import (  # noqa: F401
    BALANCED_CLI_MODEL,
    CANCEL_POLL_SECONDS,
    DEEP_REVIEW_CALL_TIMEOUT_SECONDS,
    DEEP_REVIEW_PROMPT,
    DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    ESCALATION_MARKER,
    GoalRunner,
    QAR_DEEP_TIMEOUT_SECONDS_ENV_VAR,
    SubprocessConfig,
    WEB_TOOLS,
    _is_claude_model,
    _log,
    _parse_worker_output,
    _run_goal_accepts_context_preamble,
    cli_safe_model,
    set_cli_model_translator,
    compose_goal_prompt,
    compose_goal_prompt_fitted,
    envelope_reports_no_work,
    extract_escalation_id,
    goal_settings,
    kill_process_group,
    stop_process_group,
    parse_deep_review,
    resolve_deep_review_interval_seconds,
    resolve_deep_launch,
    resolve_deep_timeout_seconds,
    resume_target_missing,
)
from .goal_runner_session import (  # noqa: F401
    _find_claude_project_dir,
    _find_session_file,
    _monitor_claude_session,
    describe_session_activity,
    landed_file_changes,
    read_session_activity_tail,
    resolve_session_file,
    resolved_model_from_session,
    session_replay_tokens,
    subagent_work_in_flight,
    worker_output_is_thin,
)


@implementation("run-deep-work", "subprocess")
class SubprocessGoalRunner(HubSubprocessGoalRunner):
    """Reference DeepRunner: spawn Claude Code headless with ``--max-turns``, with QAR's settings (see the module docstring).

    The working dir, binary, model, and any context preamble are CONFIG, not hardcoded paths. Exit code 0 = goal met cleanly;
    non-zero = hit the turn/budget limit or errored (DeepResult.met=False with a clear message). When an emit callback is
    provided, it streams live Claude Code session updates to the caller.
    """

    config_class = SubprocessConfig   # the dataclass this runner is built from (core.resolve.resolve_config)

    def __init__(self, config: SubprocessConfig, *,
                 model_translator: Optional[Callable[[Optional[str]], Optional[str]]] = None):
        super().__init__(config, model_translator=model_translator)
        self._model_translator = model_translator

    def goal_settings(self) -> GoalSettings:
        """QAR's settings, built from the environment-backed core settings on every call."""
        return goal_settings()

    def cli_safe(self, model: Optional[str]) -> Optional[str]:
        """The value to pass as ``claude --model``: this runner's translator, else the registered one, else the syntactic gate."""
        return cli_safe_model(model, self.model_translator)

    def _launch_settings(self) -> DeepLaunchSettings:
        return self.launch_settings()

    def _build_env(self, requester_user_id: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        return self.build_env(requester_user_id, run_id)

    def _call_reviewer_cli(self, prompt: str) -> str:
        return self.call_reviewer_cli(prompt)
