"""One lock per card folder, shared by every process that changes it.

Several processes share a card folder (the lanes, a terminal chat, a person running the cleanup), and
three things changed it without knowing about each other: the tidy that moves provable copies out, the
card limit that moves the coldest derived card out to make room, and the read-modify-write of an edit.
Run together they double-counted moves, overshot the limit, overwrote each other's manifests, and wrote
back a card another process had just moved. This lock makes each of those a short critical section.

``flock`` on ``<cards folder>/.store.lock`` (a dot file, so never a card), re-entrant inside a process
(one thread may take it again), and with a GENERATION: a number kept in the file that whoever changes
the folder under the lock raises by one (``bump``). A process that remembers the generation it last saw
knows, exactly and without scanning the folder, whether anybody changed it since.

Where the file system has no ``flock`` (some network mounts) the lock degrades to the in-process one and
says so once; nothing is refused.
"""
from __future__ import annotations

import errno
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional

try:  # pragma: no cover - fcntl exists on every platform the runner supports
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("quest-ai-runner.card-store")

LOCK_FILE = ".store.lock"
TIDY_LOCK_FILE = ".tidy.lock"      # held for a whole tidy, so two processes never scan and move together


class StoreLock:
    """The lock of one card folder. Use ``StoreLock.for_dir(path)``; ``with`` takes it, waiting if need be."""

    registry: Dict[str, "StoreLock"] = {}
    registry_guard = threading.Lock()

    @classmethod
    def for_dir(cls, cards_dir: str, name: str = LOCK_FILE) -> "StoreLock":
        try:
            directory = str(Path(cards_dir).resolve())
        except OSError:
            directory = str(cards_dir)
        key = f"{directory}/{name}"
        with cls.registry_guard:
            lock = cls.registry.get(key)
            if lock is None:
                lock = cls.registry[key] = cls(directory, name)
            return lock

    def __init__(self, directory: str, name: str = LOCK_FILE) -> None:
        self.directory = directory
        self.path = os.path.join(directory, name)
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.fd: Optional[int] = None
        self.warned = False

    def __enter__(self) -> "StoreLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self, blocking: bool = True) -> bool:
        """Take the lock (again, if this thread holds it). False only when ``blocking`` is off and it is busy."""
        if not self.thread_lock.acquire(blocking):
            return False
        if self.depth == 0 and not self.lock_file(blocking):
            self.thread_lock.release()
            return False
        self.depth += 1
        return True

    def release(self) -> None:
        self.depth -= 1
        if self.depth == 0:
            self.unlock_file()
        self.thread_lock.release()

    def lock_file(self, blocking: bool) -> bool:
        if fcntl is None:
            return True
        try:
            os.makedirs(self.directory, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError:
            self.warn_once("could not open the lock file")
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            self.warn_once("the file system does not support file locks")
            return True
        self.fd = fd
        return True

    def unlock_file(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None

    def warn_once(self, why: str) -> None:
        if not self.warned:
            self.warned = True
            log.warning("card folder %s: %s; changes from several processes are not serialised there",
                        self.directory, why)

    def generation(self) -> int:
        """How many times the folder has been changed under the lock (0 for a new folder)."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def bump(self) -> int:
        """Record that the folder changed. Call it while holding the lock. Returns the new generation."""
        if self.fd is None:
            return 0
        number = self.generation() + 1
        try:
            os.ftruncate(self.fd, 0)
            os.pwrite(self.fd, str(number).encode("ascii"), 0)
        except OSError:
            return 0
        return number
