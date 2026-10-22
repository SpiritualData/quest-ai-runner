"""One JSON-lines file per key in a shared directory: the keying, append, read and prune code the runner's ledgers share.

The tool-call ledger (``tools_failure_ledger``) and the metric ledger (``metric_results``) are both records "kept by the
runner and never by a model", written by two processes at once (the brain, and the deep worker's shell command), so both need
the same four things: a file per key that a reader can find from the key alone, an append that cannot interleave with
another process's append, a reader that skips a half-written or malformed line, and a prune of files untouched for days.
This module is that, once. A ledger built on it adds only its own record type.

An append is one ``os.write`` of the whole line on an ``O_APPEND`` descriptor, under an exclusive ``flock`` where the
platform has one, so two writers never produce a torn or interleaved line; the file is created with mode 0666 minus the
umask, the same permissions an ordinary ``open(path, "a")`` gives it (``file_modes`` explains why that matters on a corpus
shared by two accounts). Appends never raise: a ledger that cannot be written must not break the work it records.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping

try:  # POSIX only; elsewhere the single O_APPEND write is the guarantee
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("quest-ai-runner.jsonl_ledger")

# A ledger file untouched for this long belongs to a finished run and is removed.
LEDGER_MAX_AGE_SECONDS = 2 * 24 * 3600


class KeyedJsonlFiles:
    """An append-only JSON-lines file per key (a task id, a run id) in one directory."""

    def __init__(self, directory: str, max_age_seconds: float = LEDGER_MAX_AGE_SECONDS):
        self.directory = directory
        self.max_age_seconds = max_age_seconds

    def path_for(self, key: str) -> str:
        return os.path.join(self.directory, re.sub(r"[^A-Za-z0-9_.-]", "_", str(key)) + ".jsonl")

    def append(self, key: str, record: Mapping[str, Any]) -> bool:
        """Append one record as one line; True when it was written. Never raises."""
        if not key:
            return False
        try:
            data = (json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            os.makedirs(self.directory, exist_ok=True)
            fd = os.open(self.path_for(key), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, data)
            finally:
                os.close(fd)   # closing releases the lock
            return True
        except (OSError, TypeError, ValueError) as e:
            log.debug("ledger line not written for %s: %s", key, e)
            return False

    def read(self, key: str) -> List[Dict[str, Any]]:
        """Every well-formed record of the key, oldest first; a malformed line is skipped."""
        if not key:
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(self.path_for(key), "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return []
        return rows

    def prune(self) -> None:
        cutoff = time.time() - self.max_age_seconds
        try:
            for name in os.listdir(self.directory):
                path = os.path.join(self.directory, name)
                if name.endswith(".jsonl") and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except OSError:
            pass
