"""A built-in lane supervisor: keep one ``poll`` lane alive with no systemd and no root.

A lane is its config file. This module runs ``quest-ai-runner poll --config <toml>`` under a restart
loop (ten seconds between exits, like systemd ``Restart=always RestartSec=10``) in its own session,
and gives cron the verbs it needs: ``start``, ``stop``, ``restart``, ``status`` and an idempotent
``ensure``. State for one lane lives in one directory:

* ``lane.lock``: held (``flock``) by the live supervisor for as long as it runs. This is the truth
  about "running"; a dead supervisor releases it on its own, however it died.
* ``lane.pid``: the supervisor's pid, which is also its process group id (it leads its own session).
* ``start.lock``: serializes ``ensure``/``start`` callers so two at once start exactly one lane.
* ``lane.log``: appended by the supervisor and the lane (stdout and stderr).

Linux only (the idle check reads ``/proc``). Nothing here knows Quest.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from quest_ai_runner.the_body.configuration.lane_env_file import read_env_file

__all__ = ["LaneSpec", "default_lane_name", "default_state_dir", "default_command", "lane_status",
           "start_lane", "ensure_lane", "stop_lane", "restart_lane", "deep_run_in_flight", "supervise"]

RESTART_DELAY_SECONDS = 10.0
STOP_GRACE_SECONDS = 15.0
START_WAIT_SECONDS = 10.0
DEEP_CHILD_COMMS: Tuple[str, ...] = ("claude", "node")


@dataclass
class LaneSpec:
    """Everything needed to run one lane; ``command`` is injectable so tests supervise a fake."""
    name: str
    state_dir: Path
    command: List[str]
    env_file: Optional[Path] = None
    restart_delay: float = RESTART_DELAY_SECONDS
    extra_env: Dict[str, str] = field(default_factory=dict)

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lane.lock"

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "lane.pid"

    @property
    def start_lock_path(self) -> Path:
        return self.state_dir / "start.lock"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "lane.log"


def default_lane_name(config: Optional[str]) -> str:
    """``<parent-dir>-<stem>`` of the config path, so two lanes' ``qar.toml`` files stay distinct."""
    if not config:
        return "default"
    path = Path(config).expanduser().resolve()
    return f"{path.parent.name}-{path.stem}" if path.parent.name else path.stem


def default_state_dir(name: str, environ: Optional[Dict[str, str]] = None) -> Path:
    """``$XDG_STATE_HOME`` (else ``~/.local/state``) ``/quest-ai-runner/lanes/<name>``."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "quest-ai-runner" / "lanes" / name


def default_command(config: Optional[str]) -> List[str]:
    """The stock lane entry: ``poll`` with this config (no config: poll reads its own environment)."""
    command = [sys.executable, "-m", "quest_ai_runner.the_consciousness.terminal.cli", "poll"]
    if config:
        command += ["--config", str(Path(config).expanduser().resolve())]
    return command


def lock_is_held(lock_path: Path) -> bool:
    """True when another process holds ``lock_path``'s flock (the supervisor is alive)."""
    if not lock_path.exists():
        return False
    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def read_pid(spec: LaneSpec) -> Optional[int]:
    try:
        return int(spec.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None


def lane_status(spec: LaneSpec) -> Tuple[bool, Optional[int]]:
    """``(running, pid)``; running means a live supervisor holds the lane lock."""
    pid = read_pid(spec)
    return (lock_is_held(spec.lock_path) and pid is not None), pid


def process_group_members(pgid: int, proc_root: str = "/proc") -> List[Tuple[int, str]]:
    """``(pid, comm)`` of every live process in group ``pgid``."""
    members: List[Tuple[int, str]] = []
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            if fields[0] != "Z" and int(fields[2]) == pgid:  # a zombie is dead, only unreaped
                members.append((int(entry.name), (entry / "comm").read_text().strip()))
        except (OSError, ValueError):
            continue
    return members


def deep_run_in_flight(pgid: int, comms: Sequence[str] = DEEP_CHILD_COMMS,
                       members: Callable[[int], List[Tuple[int, str]]] = process_group_members
                       ) -> Optional[Tuple[int, str]]:
    """The ``(pid, comm)`` of a deep-run child (default ``claude`` or ``node``) in the group, if any."""
    for pid, comm in members(pgid):
        if comm in comms:
            return pid, comm
    return None


def supervisor_command(spec: LaneSpec) -> List[str]:
    """The detached supervisor process: this module's own ``main``, carrying the lane's command."""
    args = [sys.executable, "-m", "quest_ai_runner.the_consciousness.request_intake.lane_supervisor", "--state-dir", str(spec.state_dir),
            "--name", spec.name, "--restart-delay", str(spec.restart_delay)]
    if spec.env_file:
        args += ["--env-file", str(spec.env_file)]
    return args + ["--"] + spec.command


def start_lane(spec: LaneSpec, say: Callable[[str], None] = print) -> bool:
    """Start the lane if it is not running; True when running afterwards. Safe to call concurrently."""
    spec.state_dir.mkdir(parents=True, exist_ok=True)
    with open(spec.start_lock_path, "a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        running, pid = lane_status(spec)
        if running:
            say(f"{spec.name}: already running (pid {pid})")
            return True
        with open(spec.log_path, "ab") as log:
            subprocess.Popen(supervisor_command(spec), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, env={**os.environ, **spec.extra_env})
        deadline = time.monotonic() + START_WAIT_SECONDS
        while time.monotonic() < deadline:
            running, pid = lane_status(spec)
            if running:
                say(f"{spec.name}: started (pid {pid}), log {spec.log_path}")
                return True
            time.sleep(0.05)
    say(f"{spec.name}: did not start within {START_WAIT_SECONDS:.0f}s, see {spec.log_path}")
    return False


def ensure_lane(spec: LaneSpec, say: Callable[[str], None] = print) -> bool:
    """Idempotent ``start``: the verb for cron."""
    return start_lane(spec, say)


def stop_lane(spec: LaneSpec, grace: float = STOP_GRACE_SECONDS, say: Callable[[str], None] = print) -> bool:
    """TERM the whole process group, KILL it after ``grace`` seconds. True when nothing is left."""
    running, pid = lane_status(spec)
    if not running or pid is None:
        say(f"{spec.name}: not running")
        spec.pid_path.unlink(missing_ok=True)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and (lock_is_held(spec.lock_path) or process_group_members(pid)):
        time.sleep(0.05)
    if lock_is_held(spec.lock_path) or process_group_members(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 5
        while time.monotonic() < kill_deadline and process_group_members(pid):
            time.sleep(0.05)
        say(f"{spec.name}: killed after {grace:.0f}s grace (pid {pid})")
    else:
        say(f"{spec.name}: stopped (pid {pid})")
    spec.pid_path.unlink(missing_ok=True)
    return not lock_is_held(spec.lock_path)


def restart_lane(spec: LaneSpec, if_idle: bool = False, grace: float = STOP_GRACE_SECONDS,
                 comms: Sequence[str] = DEEP_CHILD_COMMS, say: Callable[[str], None] = print) -> bool:
    """Stop then start. With ``if_idle``, skip (and say so) while a deep run is in flight."""
    running, pid = lane_status(spec)
    if if_idle and running and pid is not None:
        busy = deep_run_in_flight(pid, comms)
        if busy:
            say(f"{spec.name}: deep run in flight (pid {busy[0]} {busy[1]}); skipping restart")
            return True
    if running:
        stop_lane(spec, grace, say)
    return start_lane(spec, say)


def supervise(spec: LaneSpec, say: Callable[[str], None] = print) -> int:
    """The supervisor process body: take the lock, run the command forever, restarting after each exit."""
    spec.state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(spec.lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say(f"{spec.name}: another supervisor holds the lane lock; exiting")
        return 0
    spec.pid_path.write_text(str(os.getpid()))
    child: List[Optional[subprocess.Popen]] = [None]
    stopping = [False]

    def on_term(signum, frame):
        stopping[0] = True
        if child[0] is not None and child[0].poll() is None:
            child[0].terminate()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    say(f"{spec.name}: supervisor up (pid {os.getpid()}), command: {shlex.join(spec.command)}")
    while not stopping[0]:
        env = {**os.environ, **spec.extra_env}
        if spec.env_file:
            try:
                env.update(read_env_file(spec.env_file))
            except OSError as error:
                say(f"{spec.name}: cannot read env file {spec.env_file}: {error}")
        try:
            child[0] = subprocess.Popen(spec.command, env=env, stdin=subprocess.DEVNULL)
            code = child[0].wait()
            say(f"{spec.name}: lane exited with {code}; restarting in {spec.restart_delay:g}s")
        except OSError as error:
            say(f"{spec.name}: cannot run {spec.command[0]}: {error}; retrying in {spec.restart_delay:g}s")
        deadline = time.monotonic() + spec.restart_delay
        while not stopping[0] and time.monotonic() < deadline:
            time.sleep(0.1)
    say(f"{spec.name}: supervisor stopped")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry for the detached supervisor process (started by ``start_lane``, not by hand)."""
    parser = argparse.ArgumentParser(prog="lane_supervisor")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--restart-delay", type=float, default=RESTART_DELAY_SECONDS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    def say(message: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

    spec = LaneSpec(args.name, Path(args.state_dir), command,
                    Path(args.env_file) if args.env_file else None, args.restart_delay)
    return supervise(spec, say)


if __name__ == "__main__":
    sys.exit(main())
