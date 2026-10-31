"""The goal contract, QAR's binding of the hub's (``hubofhubs.deep.contract``): the generic core is the hub's, QAR keeps its names.

Timeouts and model gating, the process-group stop, review parsing, the prompt composition and ``GoalRunner`` / ``SubprocessConfig``
are the hub's and are re-exported here under their old import path. What stays with QAR:

* the ``QAR_*`` environment names (``QAR_DEEP_TIMEOUT_SECONDS``, ``QAR_DEEP_REVIEW_INTERVAL_SECONDS``);
* the escalation marker a worker prints (``QAR-ESCALATED: <decision_id>``);
* the process-wide model translator the composition root registers (``set_cli_model_translator``);
* ``goal_settings``, which builds the hub's ``GoalSettings`` from ``core_settings()`` on every call, with QAR's usage-limit state,
  prompt budget, standing instructions, requester environment and the ``QAR_TOOL_RUN_ID`` name of a run's id in the worker's shell.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional, Tuple

from hubofhubs.deep import contract as hub
from hubofhubs.deep.contract import (  # noqa: F401  (same objects, old import path)
    BALANCED_CLI_MODEL,
    CANCEL_POLL_SECONDS,
    CLAUDE_CODE_MODEL_PATTERN,
    DEEP_REVIEW_CALL_TIMEOUT_SECONDS,
    DEEP_REVIEW_PROMPT,
    DEFAULT_CANCEL_GRACE_SECONDS,
    DEFAULT_DEEP_MAX_TURNS,
    DEFAULT_DEEP_REVIEW_MODEL,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    ESCALATION_ID_RE,
    RESUME_TARGET_MISSING_MARKERS,
    WEB_TOOLS,
    GoalRunner,
    SubprocessConfig,
    envelope_reports_no_work,
    kill_process_group,
    log as _log,
    parse_deep_review,
    parse_worker_output as _parse_worker_output,
    resume_target_missing,
    signal_process_group,
    stop_process_group,
)
from hubofhubs.deep.launch import DeepLaunchSettings
from hubofhubs.deep.settings import GoalSettings

from . import prompt_budget, standing_instructions, usage_limit
from .requester import apply_requester_env
from .runtime_settings import DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS, core_settings, process_environment

QAR_DEEP_REVIEW_INTERVAL_SECONDS_ENV_VAR = "QAR_DEEP_REVIEW_INTERVAL_SECONDS"

QAR_DEEP_TIMEOUT_SECONDS_ENV_VAR = "QAR_DEEP_TIMEOUT_SECONDS"

# The environment name that carries a run's id into the worker's shell, so a tool call that forgets ``--run`` still lands on
# this run's record (its metric result, its tool-call ledger).
RUN_ID_ENV_VAR = "QAR_TOOL_RUN_ID"

# A spawned worker that raised a human decision mid-run reports it by printing, on its own line, ``QAR-ESCALATED: <decision_id>``.
# The runner parses the marker and returns ``DeepResult(met=False, decision_id=...)``, which the executor reports as ``needs_you``
# with the decision linked. Workers that never escalate are unaffected; the marker simply never appears.
ESCALATION_MARKER = "QAR-ESCALATED:"

# Matches the marker ANYWHERE in a line (not just at its start), capturing the first whitespace-delimited token that follows it.
ESCALATION_MARKER_ANYWHERE_RE = re.compile(re.escape(ESCALATION_MARKER) + r"\s*(\S+)")

_is_claude_model = hub.is_claude_model


def goal_settings() -> GoalSettings:
    """The hub's goal settings, built from the settings in force right now (the environment-backed ``core_settings``)."""
    settings = core_settings()
    return GoalSettings(
        deep_timeout_seconds=settings.deep_timeout_seconds,
        deep_review_interval_seconds=settings.deep_review_interval_seconds,
        deep_launch=settings.deep_launch,
        claude_config_dir=settings.claude_config_dir,
        escalation_marker=ESCALATION_MARKER,
        run_id_env=RUN_ID_ENV_VAR,
        standing_instructions=lambda: standing_instructions.load_standing_instructions(),
        apply_requester=lambda env, user_id: apply_requester_env(env, user_id),
        environment=lambda: process_environment(),
        usage_limits=usage_limit,
        prompt_budget=prompt_budget,
    )


def resolve_deep_timeout_seconds(configured: Optional[float]) -> Optional[float]:
    """The OPTIONAL hard wall-clock cap for the deep subprocess, or None for no cap (``configured`` wins, else ``QAR_DEEP_TIMEOUT_SECONDS``)."""
    return hub.resolve_deep_timeout_seconds(configured, goal_settings())


def resolve_deep_launch(configured: Optional[DeepLaunchSettings]) -> DeepLaunchSettings:
    """The launch parameters of the deep worker: ``configured`` when the consumer pinned one, else the ``QAR_DEEP_*`` launch
    variables the config layer parsed into ``CoreSettings.deep_launch``. Both default to today's launch."""
    return hub.resolve_deep_launch(configured, goal_settings())


def resolve_deep_review_interval_seconds(configured: Optional[float]) -> Optional[float]:
    """Seconds between liveness reviews, or None when reviews are off (interval 0)."""
    return hub.resolve_deep_review_interval_seconds(configured, goal_settings())


def extract_escalation_id(output: str) -> Optional[str]:
    """The decision id from the LAST ``QAR-ESCALATED: <id>`` in ``output``, or None (see ``hubofhubs.deep.contract.extract_escalation_id``)."""
    return hub.extract_escalation_id(output, ESCALATION_MARKER)


def _run_goal_accepts_context_preamble(runner: Any) -> bool:
    return hub.run_goal_accepts(runner, "context_preamble")


def _run_goal_accepts_working_dir(runner: Any) -> bool:
    return hub.run_goal_accepts(runner, "working_dir")


def _run_goal_accepts_resume_session_id(runner: Any) -> bool:
    return hub.run_goal_accepts(runner, "resume_session_id")


# The model translator: ``str -> Optional[str]`` mapping a model id to the value ``claude --model`` can invoke (or None for a model
# the worker cannot run). It is INJECTED, never imported: ``core`` must not depend on ``adapters``, so the composition root registers
# the adapter's translator (``claude_cli_provider.cli_model``) with ``set_cli_model_translator``, and a runner can be handed its own
# through ``SubprocessGoalRunner(model_translator=...)``. Unset, only the syntactic ``is_claude_model`` gate applies.
_cli_model_translator: Optional[Callable[[Optional[str]], Optional[str]]] = None


def set_cli_model_translator(
        translator: Optional[Callable[[Optional[str]], Optional[str]]]) -> None:
    """Register the process-wide model translator ``cli_safe_model`` falls back to (None clears it)."""
    global _cli_model_translator
    _cli_model_translator = translator


def cli_safe_model(model: Optional[str],
                   translator: Optional[Callable[[Optional[str]], Optional[str]]] = None
                   ) -> Optional[str]:
    """The value to actually pass as ``claude --model``, or None to pass nothing.

    ``translator`` if given, else the one registered with ``set_cli_model_translator``, and with neither this degrades to the
    syntactic gate (see ``hubofhubs.deep.contract.cli_safe_model``).
    """
    return hub.cli_safe_model(model, translator or _cli_model_translator)


def compose_goal_prompt(goal: str, brief: str, *, preamble: str = "",
                        budget_tokens: Optional[int] = None,
                        model: Optional[str] = None) -> str:
    """The headless worker prompt, fitted to the deep prompt budget (see ``compose_goal_prompt_fitted``)."""
    return hub.compose_goal_prompt(goal, brief, preamble=preamble, budget_tokens=budget_tokens, model=model,
                                   settings=goal_settings())


def compose_goal_prompt_fitted(goal: str, brief: str, *, preamble: str = "",
                               budget_tokens: Optional[int] = None,
                               model: Optional[str] = None) -> Tuple[str, Any]:
    """The worker prompt and the ``prompt_budget.FitResult`` that says what, if anything, was cut (QAR's standing instructions
    and prompt budget; see ``hubofhubs.deep.contract.compose_goal_prompt_fitted``)."""
    return hub.compose_goal_prompt_fitted(goal, brief, preamble=preamble, budget_tokens=budget_tokens, model=model,
                                          settings=goal_settings())
