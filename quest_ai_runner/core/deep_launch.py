"""How the deep worker (``claude -p``) is launched: the hub's pure argv builder and frozen settings value, under the old import path.

The builder and ``DeepLaunchSettings`` are ``hubofhubs.deep.launch``. What stays with QAR is the parsing of the ``QAR_DEEP_*``
names into the value (``config_core_settings.deep_launch_from_env``) and the default it gives (today's launch, no flag added).
"""
from __future__ import annotations

from hubofhubs.deep.launch import DeepLaunchSettings, build_deep_argv, lean_flags  # noqa: F401  (same objects, old import path)
