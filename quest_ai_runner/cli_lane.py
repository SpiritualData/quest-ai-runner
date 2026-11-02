"""The `lane` subcommand: run a lane under the built-in supervisor (no systemd, no root).

Split out of ``cli.py`` by definition; that module re-exports ``run_lane_command``. The logic is
``runner/lane_supervisor.py``; this is argument handling only.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .runner.lane_supervisor import (LaneSpec, default_command, default_lane_name, default_state_dir,
                                     ensure_lane, lane_status, restart_lane, start_lane, stop_lane)


def lane_spec_from_args(args) -> LaneSpec:
    """Build the LaneSpec from `--config`, `--name`, `--state-dir`, `--env-file`, `--exec`."""
    name = args.name or default_lane_name(args.config)
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(name)
    command = shlex.split(args.exec_command) if args.exec_command else default_command(args.config)
    env_file = Path(args.env_file).expanduser().resolve() if args.env_file else None
    return LaneSpec(name, state_dir, command, env_file)


def run_lane_command(args, log) -> int:
    """Run `lane <start|stop|restart|status|ensure>`; `status` exits 1 when the lane is down."""
    if not args.config and not args.name and not args.state_dir:
        log.error("lane needs --config, or --name (a lane with no config file polls from its environment)")
        return 2
    spec = lane_spec_from_args(args)
    if args.lane_op == "status":
        running, pid = lane_status(spec)
        print(f"{spec.name}: running (pid {pid}), log {spec.log_path}" if running else f"{spec.name}: DOWN")
        return 0 if running else 1
    if args.lane_op == "stop":
        return 0 if stop_lane(spec) else 1
    if args.lane_op == "restart":
        return 0 if restart_lane(spec, if_idle=args.if_idle) else 1
    if args.lane_op == "ensure":
        return 0 if ensure_lane(spec) else 1
    return 0 if start_lane(spec) else 1
