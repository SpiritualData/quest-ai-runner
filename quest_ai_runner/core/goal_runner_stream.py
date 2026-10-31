"""Persistent stream-json deep runner, QAR's binding of the hub's (``hubofhubs.deep.stream_runner``).

One live Claude Code session per run that can be steered, stopped or paused: the runner, ``StreamRun`` and the stream protocol
helpers are the hub's. ``StreamJsonGoalRunner`` here is the hub's class on top of QAR's ``SubprocessGoalRunner`` binding, so it
runs with QAR's settings, and stays the ``run-deep-work`` implementation named ``stream_json``.
"""
from __future__ import annotations

from esoi import implementation

from hubofhubs.deep.stream_runner import (  # noqa: F401  (same objects, old import path)
    DEFAULT_ECHO_WAIT_SECONDS,
    LOOP_TICK_SECONDS,
    MIN_STREAM_CONTROL_VERSION,
    VERSION_CACHE,
    VERSION_RE,
    Sent,
    StreamJsonGoalRunner as HubStreamJsonGoalRunner,
    StreamRun,
    cli_version,
    echo_text,
    interrupt_line,
    user_line,
)

from .goal_runner import SubprocessConfig, SubprocessGoalRunner


@implementation("run-deep-work", "stream_json")
class StreamJsonGoalRunner(HubStreamJsonGoalRunner, SubprocessGoalRunner):
    """Deep runner holding one live ``claude -p --input-format stream-json`` session per run (the hub's, with QAR's settings).

    Built from the same ``SubprocessConfig`` as the one-shot runner. The extra per-call argument is ``control``, a
    ``hubofhubs.ports.control.RunControl``; without one the run is the same single prompt, only over a session that is closed when
    the model finishes."""

    config_class = SubprocessConfig
