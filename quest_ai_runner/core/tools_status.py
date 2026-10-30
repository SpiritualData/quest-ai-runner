"""The typed outcome of a tool call or a write to the work-tracking system.

One word says what happened, so no reader has to guess it from free text:

``applied``  the change was made (or the read ran).
``held``     the system took the request but it WAITS for a person's approval; nothing changed yet.
``noop``     the thing already existed or already held, so nothing needed to change.
``refused``  the system understood and said no (not allowed, invalid, a conflict); the detail says why.
``failed``   it did not happen (the system was unreachable, errored, or answered too slowly to count).
``unknown``  the request was sent and no answer came back, so it MAY have happened.

``applied`` and ``noop`` mean the end state holds. ``held``, ``refused``, ``failed`` and ``unknown`` mean the
end state does NOT (yet) hold, and a reply must never say it does. The status always comes from the tool's own
result or the HTTP exchange, never from reading words in model output (hard rule #3).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ToolStatus(str, Enum):
    APPLIED = "applied"
    HELD = "held"
    NOOP = "noop"
    REFUSED = "refused"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: Statuses whose end state holds.
DONE_STATUSES = frozenset({ToolStatus.APPLIED, ToolStatus.NOOP})

#: Statuses where the request reached Quest and was taken (nothing to retry, nothing went wrong): the end state holds, or
#: it is held for a person. A caller that only asks "was my request accepted?" tests this, so a held proposal is not a failure.
ACCEPTED_STATUSES = frozenset({ToolStatus.APPLIED, ToolStatus.NOOP, ToolStatus.HELD})

#: Statuses whose write must never be sent again by the runner on its own: it was applied, was already so, is waiting
#: for a person, or MAY have happened (sent, no answer).
SETTLED_STATUSES = frozenset({ToolStatus.APPLIED, ToolStatus.NOOP, ToolStatus.HELD, ToolStatus.UNKNOWN})

#: The word a receipt line carries for each status (the first word after ``TOOL <name>``).
RECEIPT_WORDS = {
    ToolStatus.APPLIED: "SUCCEEDED",
    ToolStatus.NOOP: "NO CHANGE NEEDED",
    ToolStatus.HELD: "HELD FOR APPROVAL",
    ToolStatus.REFUSED: "REFUSED",
    ToolStatus.FAILED: "FAILED",
    ToolStatus.UNKNOWN: "OUTCOME UNKNOWN",
}


def as_status(value: Any, default: Optional[ToolStatus] = None) -> Optional[ToolStatus]:
    """``value`` as a ``ToolStatus`` (a member or its string), else ``default``."""
    if isinstance(value, ToolStatus):
        return value
    try:
        return ToolStatus(str(value).strip().lower())
    except ValueError:
        return default


def status_of_error(error: BaseException) -> ToolStatus:
    """The status a raised error carries (``write_status`` on a client write error), else ``failed``."""
    return as_status(getattr(error, "write_status", None), ToolStatus.FAILED)


def classify_payload(payload: Any, http_status: Optional[int] = None) -> ToolStatus:
    """The status of a write from what the server SENT: its HTTP status and the flags in its answer.

    ``held`` when Quest took the request but is waiting on a person (HTTP 202, ``applied`` false, ``pending_approval``
    or ``pending`` true); ``noop`` when it says the thing already existed (``already_existed``, ``duplicate`` true);
    otherwise ``applied``. Only structured fields are read, never the wording of a message."""
    if http_status == 202:
        return ToolStatus.HELD
    if isinstance(payload, dict):
        if payload.get("applied") is False or payload.get("pending_approval") or payload.get("pending") is True:
            return ToolStatus.HELD
        if payload.get("already_existed") or payload.get("duplicate") is True:
            return ToolStatus.NOOP
    return ToolStatus.APPLIED


def write_status_of(result: Any) -> ToolStatus:
    """The status of what a client write returned: a typed result carries it, a plain payload is classified."""
    status = as_status(getattr(result, "status", None))
    return status if status is not None else classify_payload(result)
