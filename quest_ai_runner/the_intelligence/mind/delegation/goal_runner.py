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

from hubofhubs.api import DeepLaunchSettings
from hubofhubs.api import GoalSettings
from hubofhubs.api import DeepResult, DeepRunner, LAUNCH_MISSING_PROGRAM, LAUNCH_PERMISSION_DENIED, ProgressEvent  # noqa: F401
from hubofhubs.api import EVENT_EXEC  # noqa: F401
from hubofhubs.api import SubprocessGoalRunner as HubSubprocessGoalRunner

from quest_ai_runner.the_intelligence.mind.delegation.goal_runner_receipts import receipt_ids, receipt_lines, tool_records
from quest_ai_runner.the_intelligence.mind.delegation.goal_runner_contract import (
    BALANCED_CLI_MODEL, CANCEL_POLL_SECONDS, DEEP_REVIEW_CALL_TIMEOUT_SECONDS, DEEP_REVIEW_PROMPT, DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS, ESCALATION_MARKER, GoalRunner, QAR_DEEP_TIMEOUT_SECONDS_ENV_VAR, SubprocessConfig, WEB_TOOLS,
    _is_claude_model, _log, _parse_worker_output, _run_goal_accepts_context_preamble, cli_safe_model, set_cli_model_translator,
    compose_goal_prompt, compose_goal_prompt_fitted, envelope_reports_no_work, extract_escalation_id, goal_settings, kill_process_group,
    stop_process_group, parse_deep_review, resolve_deep_review_interval_seconds, resolve_deep_launch, resolve_deep_timeout_seconds,
    resume_target_missing)
from quest_ai_runner.the_intelligence.mind.delegation.goal_runner_session import (
    _find_claude_project_dir, _find_session_file, _monitor_claude_session, describe_session_activity, landed_file_changes,
    read_session_activity_tail, resolve_session_file, resolved_model_from_session, session_replay_tokens, subagent_work_in_flight,
    worker_output_is_thin)


@implementation("delegated-agent-sessions", "subprocess")
class SubprocessGoalRunner(HubSubprocessGoalRunner):
    """Reference DeepRunner: spawn Claude Code headless with ``--max-turns``, with QAR's settings (see the module docstring).

    The working dir, binary, model, and any context preamble are CONFIG, not hardcoded paths. Exit code 0 = goal met cleanly;
    non-zero = hit the turn/budget limit or errored (DeepResult.met=False with a clear message). When an emit callback is
    provided, it streams live Claude Code session updates to the caller.
    """

    config_class = SubprocessConfig   # the dataclass this runner is built from (core.resolve.resolve_config)

    def __init__(self, config: SubprocessConfig, *,
                 model_translator: Optional[Callable[[Optional[str]], Optional[str]]] = None,
                 provider: str = "anthropic", tier: str = ""):
        # ``provider`` and ``tier`` name the hub arm this runner is (see ``hubofhubs.api.choose_agent_arm``); the tier's model is
        # used only when a call names no model, so the deep-model ladder still picks the model of every run it starts.
        super().__init__(config, model_translator=model_translator, provider=provider, tier=tier)
        self._model_translator = model_translator

    def goal_settings(self) -> GoalSettings:
        """QAR's settings, built from the environment-backed core settings on every call."""
        return goal_settings()

    def cli_safe(self, model: Optional[str]) -> Optional[str]:
        """The value to pass as ``claude --model``: this runner's translator, else the registered one, else the syntactic gate."""
        return cli_safe_model(model, self.model_translator)

    def landed_in(self, working_dir: Optional[str], session_id: Optional[str]):
        """The hub's already-landed set, plus the tagged ids of every tool use the session holds, so a resumed run's receipts
        count only its own work (``core/goal_runner_receipts.py``)."""
        landed = super().landed_in(working_dir, session_id)
        if not session_id:
            return landed
        records = tool_records(self.cfg.working_dir if working_dir is None else working_dir, session_id,
                               self.goal_settings().claude_config_dir)
        return set(landed) | receipt_ids(records)

    def count_landed(self, res: DeepResult, working_dir: Optional[str], already) -> None:
        """The hub's ``changes_landed`` count, then the run's receipts: what it did, counted from its session record and
        never from its prose. They become ``observations`` (``observations_reported`` True) so the interim policy can say what
        was done. A run whose record cannot be read keeps the hub's behaviour (no receipts, nothing reported)."""
        super().count_landed(res, working_dir, already)
        if not res.session_id:
            return
        records = tool_records(self.cfg.working_dir if working_dir is None else working_dir, res.session_id,
                               self.goal_settings().claude_config_dir)
        if not records:
            return
        res.observations = list(res.observations or []) + receipt_lines(records, already)
        res.observations_reported = True
