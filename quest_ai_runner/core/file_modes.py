"""file_modes — give an atomically written file the permissions a normal write would have had.

Every store here writes through ``tempfile.mkstemp`` + ``os.replace`` so a reader never sees half a
file. But ``mkstemp`` ALWAYS creates the file as 0600, whatever the process umask, and ``os.replace``
keeps that mode. On a corpus shared by more than one account (two people's runners over the same
folder, say), every card one of them wrote was unreadable to the other, silently: the other side's
card loader skips a file it cannot open, so the context just went missing. Found 2026-09-26 with
seven quest-management cards written by one account that the other account's chat could not read.

Call :func:`match_umask` on the temp file's descriptor before the replace, and the result is what
``open(path, "w")`` would have produced: 0666 minus the umask (0644 or 0664 in practice).
"""
from __future__ import annotations

import os
import threading
from typing import Optional

umask_lock = threading.Lock()
cached_umask: Optional[int] = None


def current_umask() -> int:
    """The process umask. Read from /proc where it exists; otherwise set-and-restore under a lock."""
    global cached_umask
    if cached_umask is not None:
        return cached_umask
    value = None
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    value = int(line.split()[1], 8)
                    break
    except (OSError, ValueError, IndexError):
        value = None
    if value is None:
        with umask_lock:
            value = os.umask(0o022)
            os.umask(value)
    cached_umask = value
    return value


def match_umask(fd: int) -> None:
    """chmod an open file to 0666 minus the umask. Best-effort: never raises."""
    try:
        os.fchmod(fd, 0o666 & ~current_umask())
    except (OSError, AttributeError):
        pass
