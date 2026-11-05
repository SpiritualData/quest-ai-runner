"""Who a piece of AI work is being done for, handed to everything the runner spawns.

The runner acts on behalf of a Quest user: the person who sent a chat turn, or who created a queued
task. A spawned process (a Claude Code deep worker, a command tool) cannot ask the runner who that
is, so the runner puts the id in its environment as ``QAR_REQUESTER_USER_ID``. An operator's own
hooks and tools can then decide what that person may do, for example refuse writes to a repository
that only its owner may change. The variable is generic: the library attaches no meaning to the id.

An unknown requester REMOVES the variable from the child environment rather than inheriting one
from the runner's own process, so a stale value can never be attributed to the wrong person.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

REQUESTER_ENV = "QAR_REQUESTER_USER_ID"


def requester_from_task(task: Mapping[str, Any]) -> Optional[str]:
    """The user a queued task was created by (``created_by``), else its owner (``user_id``)."""
    for key in ("created_by", "user_id"):
        value = task.get(key)
        if value:
            return str(value)
    return None


def requester_from_meta(meta: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The requester of a turn: an explicit ``requester_user_id``, else the turn's ``user_id``."""
    for key in ("requester_user_id", "user_id"):
        value = (meta or {}).get(key)
        if value:
            return str(value)
    return None


def apply_requester_env(env: Dict[str, str], user_id: Optional[str]) -> Dict[str, str]:
    """Set (or clear) ``QAR_REQUESTER_USER_ID`` in a child environment, in place."""
    if user_id:
        env[REQUESTER_ENV] = str(user_id)
    else:
        env.pop(REQUESTER_ENV, None)
    return env
