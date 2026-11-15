"""The thread-local that records which AI rep (persona) the current task resolved to.

``runner.personas`` writes it when it resolves a task's persona; anything running inside the task
(an adapter's context assembler, say) reads it back with ``current_rep()``. It lives in ``core`` so
adapters can read it without importing ``runner``; ``runner.personas`` re-exports both names.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

# WHICH persona the task on THIS worker thread resolved to, or None.
#
# Thread-local because the executor runs tasks on a pool and the resolver fires once per task, on
# the same thread that then runs it. It is written on EVERY resolution attempt, including the
# failures, so a task that resolves to nobody clears whatever the previous task on this thread
# left behind -- without that, one task's persona leaks into the next one's context.
_CURRENT_REP = threading.local()


def current_rep() -> Optional[Dict[str, Any]]:
    """The persona this thread's current task resolved to (``{task, user_id, skill_dir}``), or None.

    The read side of what ``build_persona_resolver`` stashes. Anything that runs inside a task and
    needs to know who it is running as reads it here rather than requiring the consumer to thread
    an ``on_resolved`` callback into a thread-local of its own.
    """
    return getattr(_CURRENT_REP, "value", None)
