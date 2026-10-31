"""How the deep worker (``claude -p``) is launched: the hub's pure argv builder and frozen settings value, under the old import path.

The builder and ``DeepLaunchSettings`` are ``hubofhubs.deep.launch``. What stays with QAR is the parsing of the ``QAR_DEEP_*``
names into the value (``config_core_settings.deep_launch_from_env``) and the default it gives (today's launch, no flag added).
"""
from __future__ import annotations

from hubofhubs.deep.launch import DeepLaunchSettings, build_deep_argv, lean_flags  # noqa: F401  (same objects, old import path)

# Named launch profiles, selected by ``QAR_DEEP_LAUNCH_PROFILE`` (config_core_settings). Unset, the launch is today's.
#
# ``lean_guarded``: the lean flags that cut the worker's fixed prompt overhead, minus the two that were found unsafe on a
# deployment that runs the worker with ``--dangerously-skip-permissions`` (measured 2026-10-31, README of
# ``evaluation/components/deep_runner/launch_experiment``):
#   * setting sources stay ``user`` (not none), because the user settings carry the hooks that guard the worker's writes and
#     deletes and the model overrides; ``--setting-sources ""`` silently removed them;
#   * the web tools stay in the tool list, because the config advertises web whenever no allowlist is pinned;
#   * no spend ceiling, because a ceiling would end a long goal that today finishes; add ``QAR_DEEP_MAX_BUDGET_USD`` to set one.
LEAN_GUARDED = DeepLaunchSettings(
    tools=("Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch"),
    setting_sources="user", strict_mcp=True, exclude_dynamic_sections=True)

PROFILES = {"lean_guarded": LEAN_GUARDED}
