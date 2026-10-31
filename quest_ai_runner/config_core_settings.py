"""Resolve the tunables ``core`` uses (``core.runtime_settings.CoreSettings``) from the environment.

This is the config layer's half of the S13 rule: ``core`` never reads ``os.environ``, so the QAR_*
variables it used to read are parsed here, with the same defaults and the same tolerance for bad
values, into one ``CoreSettings``. ``RunnerConfig.core_settings`` pins a fixed value; left None, the
package installs ``core_settings_from_env`` as the provider, which re-reads the environment on every
call so a running process can be retuned without a restart, exactly as before.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Mapping, Optional

from .config_support import _log
from .core.deep_launch import PROFILES, DeepLaunchSettings
from .core.runtime_settings import (
    DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS,
    CoreSettings,
    install_core_settings,
    install_process_environment,
)


def _float_or(raw: Optional[str], default: float, *, positive: bool = True) -> float:
    """A float from ``raw``; blank, malformed (and, when ``positive``, non-positive) give ``default``."""
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if (value > 0 or not positive) else default


def _optional_positive(raw: Optional[str], name: str) -> Optional[float]:
    """A positive float, or None for unset, malformed (logged) or non-positive."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _log.warning("%s=%r is not a valid number, ignoring it", name, raw)
        return None
    return value if value > 0 else None


_TRUE = ("1", "true", "yes", "on")


def deep_launch_from_env(env: Mapping[str, str]) -> DeepLaunchSettings:
    """The deep worker's launch parameters the environment asks for; unset or blank leaves a parameter at today's launch.

    ``QAR_DEEP_TOOLS``: comma-separated built-in tool names (``--tools``); ``none`` is no tools at all.
    ``QAR_DEEP_SETTING_SOURCES``: comma-separated ``user``, ``project``, ``local`` (``--setting-sources``); ``none`` loads none.
    ``QAR_DEEP_STRICT_MCP`` and ``QAR_DEEP_EXCLUDE_DYNAMIC_SECTIONS``: ``1`` adds ``--strict-mcp-config`` and
    ``--exclude-dynamic-system-prompt-sections``. ``QAR_DEEP_MAX_BUDGET_USD``: a positive number (``--max-budget-usd``).
    ``QAR_DEEP_LAUNCH_PROFILE``: a named bundle of these (``core.deep_launch.PROFILES``, today ``lean_guarded``); a name that is
    not known is logged and ignored, and any ``QAR_DEEP_*`` name above that is set overrides the profile's field.
    The turn cap is the existing ``QAR_DEEP_MAX_TURNS`` (``OrchestratorConfig.deep_max_turns``), not a second variable.
    """
    profile_name = (env.get("QAR_DEEP_LAUNCH_PROFILE") or "").strip().lower()
    base = DeepLaunchSettings()
    if profile_name:
        if profile_name in PROFILES:
            base = PROFILES[profile_name]
        else:
            _log.warning("QAR_DEEP_LAUNCH_PROFILE=%r is not one of %s, using today's launch", profile_name, sorted(PROFILES))
    tools_raw = (env.get("QAR_DEEP_TOOLS") or "").strip()
    tools = base.tools if not tools_raw else (() if tools_raw.lower() == "none" else
                                               tuple(t.strip() for t in tools_raw.split(",") if t.strip()))
    sources_raw = (env.get("QAR_DEEP_SETTING_SOURCES") or "").strip()
    sources = base.setting_sources if not sources_raw else ("" if sources_raw.lower() == "none" else sources_raw)
    strict_raw = (env.get("QAR_DEEP_STRICT_MCP") or "").strip().lower()
    dynamic_raw = (env.get("QAR_DEEP_EXCLUDE_DYNAMIC_SECTIONS") or "").strip().lower()
    budget = _optional_positive(env.get("QAR_DEEP_MAX_BUDGET_USD"), "QAR_DEEP_MAX_BUDGET_USD")
    return dataclasses.replace(
        base, tools=tools, setting_sources=sources,
        strict_mcp=(strict_raw in _TRUE) if strict_raw else base.strict_mcp,
        exclude_dynamic_sections=(dynamic_raw in _TRUE) if dynamic_raw else base.exclude_dynamic_sections,
        max_budget_usd=budget if budget is not None else base.max_budget_usd)


def core_settings_from_env(env: Optional[Mapping[str, str]] = None) -> CoreSettings:
    """The ``CoreSettings`` the environment (default: the process environment) asks for."""
    env = os.environ if env is None else env
    d = CoreSettings()

    budget: Optional[int] = None
    raw_budget = (env.get("QAR_DEEP_PROMPT_TOKEN_BUDGET") or "").strip()
    if raw_budget:
        try:
            budget = int(raw_budget)
        except ValueError:
            _log.warning("QAR_DEEP_PROMPT_TOKEN_BUDGET=%r is not an integer; using the default", raw_budget)

    review_raw = (env.get("QAR_DEEP_REVIEW_INTERVAL_SECONDS") or "").strip()
    review = (_optional_positive(review_raw, "QAR_DEEP_REVIEW_INTERVAL_SECONDS")
              if review_raw else DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS)

    gate_raw = env.get("QAR_NOTE_FLOOR_RELEVANCE_GATED")
    gated = d.note_floor_relevance_gated if gate_raw is None or not gate_raw.strip() else \
        gate_raw.strip().lower() not in ("0", "false", "no", "off")

    verify_raw = env.get("QAR_VERIFY_CONTEXT_MAX_CHARS")
    verify = d.verify_context_max_chars
    if verify_raw is not None and verify_raw.strip():
        try:
            verify = int(verify_raw) if int(verify_raw) > 0 else d.verify_context_max_chars
        except ValueError:
            pass

    return CoreSettings(
        read_op_timeout_seconds=_float_or(env.get("QAR_READ_OP_TIMEOUT_SECONDS"), d.read_op_timeout_seconds),
        quest_judge_timeout_seconds=_float_or(
            env.get("QAR_QUEST_JUDGE_TIMEOUT_SECONDS"), d.quest_judge_timeout_seconds),
        intent_judge_timeout_seconds=_float_or(
            env.get("QAR_INTENT_JUDGE_TIMEOUT_SECONDS"), d.intent_judge_timeout_seconds),
        mode_release_timeout_seconds=_float_or(
            env.get("QAR_MODE_RELEASE_TIMEOUT_SECONDS"), d.mode_release_timeout_seconds),
        context_assembly_timeout_seconds=_float_or(
            env.get("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS"), d.context_assembly_timeout_seconds),
        guidance_selection_timeout_seconds=_float_or(
            env.get("QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS"), d.guidance_selection_timeout_seconds),
        verify_context_max_chars=verify,
        note_floor_relevance_gated=gated,
        note_floor_fresh_minutes=_float_or(
            env.get("QAR_NOTE_FLOOR_FRESH_MINUTES"), d.note_floor_fresh_minutes, positive=False),
        deep_prompt_token_budget=budget,
        deep_timeout_seconds=_optional_positive(env.get("QAR_DEEP_TIMEOUT_SECONDS"), "QAR_DEEP_TIMEOUT_SECONDS"),
        deep_review_interval_seconds=review,
        deep_launch=deep_launch_from_env(env),
        deep_instructions_files=env.get("QAR_DEEP_INSTRUCTIONS_FILES"),
        link_policy_file=env.get("QAR_LINK_POLICY_FILE", ""),
        claude_config_dir=env.get("CLAUDE_CONFIG_DIR"),
        metric_meters=(env.get("QAR_METRIC_METERS") or d.metric_meters).strip(),
        metric_arm_timeout_seconds=_float_or(
            env.get("QAR_METRIC_ARM_TIMEOUT_SECONDS"), d.metric_arm_timeout_seconds),
    )


def install_environment_backed_core() -> None:
    """Make core read the live process environment through this layer (what a plain import does)."""
    install_core_settings(core_settings_from_env)
    install_process_environment(lambda: dict(os.environ))


def install_for_config(core_settings: Optional[CoreSettings]) -> None:
    """Pin ``RunnerConfig.core_settings`` into core when the consumer set one."""
    if core_settings is not None:
        install_core_settings(core_settings)
