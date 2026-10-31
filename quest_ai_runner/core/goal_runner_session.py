"""Reading the worker's own session record, QAR's binding of the hub's (``hubofhubs.deep.session``).

The live monitor, the resume lookups and the end-of-run diagnostics are the hub's. The one value they need from the host is where
Claude Code keeps its sessions (``CLAUDE_CONFIG_DIR``); the functions here supply it from ``core_settings()`` on every call, so a
caller keeps the old argument lists.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Set

from hubofhubs.deep import session as hub
from hubofhubs.deep.session import (  # noqa: F401  (same objects, old import path)
    FILE_CHANGE_TOOLS,
    SESSION_TAIL_MAX_ACTIONS,
    SESSION_TAIL_MAX_BYTES,
    SESSION_TAIL_MAX_CHARS,
    SESSION_TAIL_THIN_OUTPUT_CHARS,
    session_record_paths,
    subagent_work_in_flight,
    worker_output_is_thin,
)
from hubofhubs.ports.deep import ProgressEvent

from .goal_runner_contract import DEFAULT_MONITOR_HEARTBEAT_SECONDS
from .runtime_settings import core_settings

_truncate_keep_end = hub.truncate_keep_end
_format_message_text = hub.format_message_text


def config_dir() -> Optional[str]:
    return core_settings().claude_config_dir


def _claude_project_bases(working_dir: Optional[str] = None) -> List[Path]:
    """Every ``projects`` directory a Claude Code session could be written under, best first."""
    return hub.claude_project_bases(working_dir, config_dir())


def _find_claude_project_dir(working_dir: Optional[str] = None) -> Optional[Path]:
    """Find a .claude/projects directory that holds JSONL files."""
    return hub.find_claude_project_dir(working_dir, config_dir())


def _find_session_file(session_id: str, working_dir: Optional[str] = None) -> Optional[Path]:
    """The JSONL of the session launched with ``--session-id``, searched under EVERY base."""
    return hub.find_session_file(session_id, working_dir, config_dir())


def session_file_size(working_dir: Optional[str], session_id: Optional[str]) -> int:
    """Bytes already in the session's file, 0 when it has none yet. Never raises."""
    return hub.session_file_size(working_dir, session_id, config_dir())


def _monitor_claude_session(
    working_dir: str,
    callback: Callable[[ProgressEvent], None],
    stop_event: threading.Event,
    cutoff_time: Optional[float] = None,
    poll_interval: float = 0.1,
    max_wait_seconds: float = 30.0,
    forced_run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    heartbeat_interval: float = DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    start_offset: int = 0,
) -> None:
    """Monitor a Claude Code session file and stream updates via callback (see ``hubofhubs.deep.session.monitor_claude_session``)."""
    hub.monitor_claude_session(working_dir, callback, stop_event, cutoff_time, poll_interval, max_wait_seconds, forced_run_id,
                               session_id, heartbeat_interval, start_offset, config_dir())


def resolve_session_file(working_dir: Optional[str], session_id: Optional[str]) -> Optional[Path]:
    """The session JSONL file for ``session_id``, or None if it can't be located. Never raises."""
    return hub.resolve_session_file(working_dir, session_id, config_dir())


def session_replay_tokens(working_dir: Optional[str], session_id: Optional[str]) -> Optional[int]:
    """About how many tokens resuming ``session_id`` would replay ahead of the next prompt, or None. Never raises."""
    return hub.session_replay_tokens(working_dir, session_id, config_dir())


def resolved_model_from_session(working_dir: Optional[str], session_id: Optional[str],
                                *, max_bytes: int = 400_000) -> Optional[str]:
    """The full model id Claude Code actually ran this session with, or None. Never raises."""
    return hub.resolved_model_from_session(working_dir, session_id, config_dir(), max_bytes=max_bytes)


def read_session_activity_tail(working_dir: Optional[str], session_id: Optional[str], **limits: int) -> str:
    """The last few human-readable actions from this run's session record, most recent last, or ""."""
    return hub.read_session_activity_tail(working_dir, session_id, config_dir(), **limits)


def describe_session_activity(working_dir: Optional[str], session_id: Optional[str], *, worker_output: str = "") -> str:
    """A ready-to-append "what this run actually did" block, or "" when nothing is readable."""
    return hub.describe_session_activity(working_dir, session_id, config_dir(), worker_output=worker_output)


def landed_file_changes(working_dir: Optional[str], session_id: Optional[str]) -> Set[str]:
    """The ids of the tool uses in the session's record that changed a file and finished without an error."""
    return hub.landed_file_changes(working_dir, session_id, config_dir())
