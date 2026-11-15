"""Config support: the logger and the tri-state sentinels RunnerConfig defaults to.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging


_log = logging.getLogger("quest-ai-runner.context")


# Sentinel default for RunnerConfig.context_assembler: "build the default FileContextStore".
# Distinct from None (which means context handling is explicitly disabled) and from an instance
# (use that one). Lets context handling be ON BY DEFAULT while staying overridable and disableable.
_AUTO_CONTEXT = object()


# Sentinel default for RunnerConfig.deep_runner: "build the default SubprocessGoalRunner (Claude
# Code)". Exactly the ``_AUTO_CONTEXT`` tri-state, for exactly the same reason: execution is core
# to running well, so leaving the field unset must mean "give me the working default", not "run
# with no executor at all". Before this existed the field defaulted to ``None``, which made
# "the consumer never wired one" indistinguishable from "the consumer deliberately disabled
# execution" — so a consumer that simply forgot silently lost every deep/execution turn (the turn
# still announced work, then nothing ran).
_AUTO_DEEP_RUNNER = object()
