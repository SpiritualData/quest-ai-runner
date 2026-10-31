"""Runtime settings the brain needs, supplied by the config layer instead of read from the process.

``core`` never touches ``os.environ`` (structure rule S13). The tunables it used to read from QAR_*
variables (timeouts, caps, floors, file lists) live here as one typed ``CoreSettings`` value with
the shipped defaults. The config layer builds that value (``config_core_settings``, field
``RunnerConfig.core_settings``) and installs it with ``install_core_settings``; the package
``__init__`` installs an environment-backed provider so a plain import keeps the old behaviour of
reading the current environment on every call, which lets a deployment or a test retune a running
process without a restart.

Core code calls ``core_settings()`` at the point of use. The provider is a callable so a value is
resolved fresh each time unless a consumer pins a fixed ``CoreSettings``.

``process_environment()`` is the same idea for the one place core must hand the whole environment
to a child process or to a tool catalog: the config layer injects the source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Union

from .deep_launch import DeepLaunchSettings

DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class CoreSettings:
    """Every tunable core reads. Defaults here are the shipped behaviour (env var in brackets)."""

    read_op_timeout_seconds: float = 60.0  # [QAR_READ_OP_TIMEOUT_SECONDS]
    quest_judge_timeout_seconds: float = 8.0  # [QAR_QUEST_JUDGE_TIMEOUT_SECONDS]
    intent_judge_timeout_seconds: float = 8.0  # [QAR_INTENT_JUDGE_TIMEOUT_SECONDS]
    mode_release_timeout_seconds: float = 8.0  # [QAR_MODE_RELEASE_TIMEOUT_SECONDS]
    context_assembly_timeout_seconds: float = 30.0  # [QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS]
    guidance_selection_timeout_seconds: float = 15.0  # [QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS]
    verify_context_max_chars: int = 24000  # [QAR_VERIFY_CONTEXT_MAX_CHARS]
    note_floor_relevance_gated: bool = True  # [QAR_NOTE_FLOOR_RELEVANCE_GATED]
    note_floor_fresh_minutes: float = 5.0  # [QAR_NOTE_FLOOR_FRESH_MINUTES]
    deep_prompt_token_budget: Optional[int] = None  # [QAR_DEEP_PROMPT_TOKEN_BUDGET]
    deep_timeout_seconds: Optional[float] = None  # [QAR_DEEP_TIMEOUT_SECONDS]; None = no cap
    # [QAR_DEEP_REVIEW_INTERVAL_SECONDS]; None = reviews off
    deep_review_interval_seconds: Optional[float] = DEFAULT_DEEP_REVIEW_INTERVAL_SECONDS
    # [QAR_DEEP_INSTRUCTIONS_FILES]; None = the shipped default, "" = none
    deep_instructions_files: Optional[str] = None
    link_policy_file: str = ""  # [QAR_LINK_POLICY_FILE]
    claude_config_dir: Optional[str] = None  # [CLAUDE_CONFIG_DIR]
    # How the deep worker is launched: the lean flags, a spend ceiling and a turn override. The default adds no flag, so it is
    # today's launch [QAR_DEEP_TOOLS, QAR_DEEP_SETTING_SOURCES, QAR_DEEP_STRICT_MCP, QAR_DEEP_EXCLUDE_DYNAMIC_SECTIONS,
    # QAR_DEEP_MAX_BUDGET_USD, QAR_DEEP_LAUNCH_PROFILE]
    deep_launch: DeepLaunchSettings = DeepLaunchSettings()
    # Which meters read a metric's value, in the order tried (core/metric_meter.py) [QAR_METRIC_METERS]
    metric_meters: str = "recorded_row,offline_arm"
    # The longest ONE reading of a metric may take, in all (an offline arm is stopped at it); a reading is taken before
    # a deep worker starts, so it is short [QAR_METRIC_ARM_TIMEOUT_SECONDS]
    metric_arm_timeout_seconds: float = 15.0


SettingsSource = Union[CoreSettings, Callable[[], CoreSettings]]

_settings_source: SettingsSource = CoreSettings
_environment_source: Callable[[], Mapping[str, str]] = dict


def install_core_settings(source: SettingsSource) -> None:
    """Use ``source`` (a fixed ``CoreSettings`` or a zero-argument callable returning one)."""
    global _settings_source
    _settings_source = source


def core_settings() -> CoreSettings:
    """The settings in force right now."""
    src = _settings_source
    return src if isinstance(src, CoreSettings) else src()


def install_process_environment(source: Callable[[], Mapping[str, str]]) -> None:
    """Register where core gets the process environment for child processes and tool catalogs."""
    global _environment_source
    _environment_source = source


def process_environment() -> Mapping[str, str]:
    """A snapshot of the environment the config layer injected (empty when none was)."""
    return _environment_source()
