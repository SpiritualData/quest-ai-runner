"""The lane subcommands: `poll` (the default) and `channel`.

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os

from .runner.channel_runner import ChannelRunner
from .runner.poller import Poller
from .cli_config import _apply_channel_env, _config_from_env


def _cmd_channel(args, log) -> int:
    """Run the `channel` subcommand; returns the process exit code."""
    cfg = _config_from_env(getattr(args, "config", None))
    try:
        _apply_channel_env(cfg)
    except ValueError as e:
        log.error("channel config error: %s", e)
        return 1
    # A channel is a conversational lane like `chat`: Quest credentials and a retrieval
    # adapter are optional (no corpus = no grounding, but the turn still runs).
    _skip = {"quest", "retrieval adapter", "team_id"}
    problems = [p for p in cfg.validate() if not any(kw in p for kw in _skip)]
    if problems:
        for p in problems:
            log.error("config error: %s", p)
        return 1
    if cfg.channel_transport is None:
        log.error("no channel transport configured — set QAR_CHANNEL_PROVIDER=openclaw and "
                 "QAR_OPENCLAW_TOKEN_FILE (see docs/live-channels.md)")
        return 1
    if not cfg.channel_allowed_senders:
        log.warning("QAR_CHANNEL_ALLOWED_SENDERS is empty — every sender will be REJECTED "
                   "(fail closed). This is very likely not what you want; set it to the "
                   "sender id(s) allowed to reach this runner.")
    runner = ChannelRunner(cfg)
    if getattr(args, "check", False):
        log.info("channel config OK: transport=%s allowed_senders=%d",
                 cfg.channel_transport.channel, len(cfg.channel_allowed_senders))
        return 0
    if getattr(args, "once", False):
        dispatched = runner.run_once()
        log.info("channel: dispatched %d message(s)", dispatched)
        return 0
    runner.run_forever()
    return 0


def _cmd_poll(args, log) -> int:
    """Run the poller (the default when no subcommand is given, and `poll`)."""
    cfg = _config_from_env(getattr(args, "config", None))
    lane = cfg.lane_label or cfg.runner_label or "runner"
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.info("%s lane config incomplete: %s", lane, p)
        return 0

    # The config file's own state_path is the last resort BELOW the env var, matching every other
    # field's precedence, so a unit can still relocate one lane's state without touching its file.
    state_path = os.getenv("QAR_STATE_PATH") or cfg.state_path or "qar_state.json"
    poller = Poller(cfg, state_path=state_path)

    once = args.once or (args.command == "poll" and getattr(args, "once", False))
    check = args.check or (args.command == "poll" and getattr(args, "check", False))

    if check:
        try:
            who = poller.client.whoami()
            log.info("%s key OK: %s", lane, who)
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("%s key check failed: %s", lane, e)
            return 1

    if once:
        handled = poller.run_once()
        log.info("handled %d task(s)", len(handled))
        return 0

    poller.run_forever()
    return 0
