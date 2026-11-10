"""The values of ``digest_by_need``: the records a need is read from, the digest of each group, and the cache a digest is kept in.

Nothing here knows where records live. A host (the Quest backend) turns its own collections, notes and insights into ``SourceGroup`` values whose
``loader`` pages the records in when the group is read; the digest engine only sees units of text under ids it can cite.
"""
from __future__ import annotations

import threading
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

STATUS_ANALYSED = "analysed"
STATUS_CACHED = "cached"
STATUS_NOTHING = "nothing_relevant"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_DIRECT = "direct"
#: The statuses that mean a group was not fully read: the result is partial and says why.
UNREAD_STATUSES = (STATUS_FAILED, STATUS_SKIPPED)


@dataclass(frozen=True)
class Unit:
    """One record a finding can cite: ``source_id`` is the only identity the engine knows it by."""
    source_id: str
    title: str
    text: str


@dataclass
class SourceGroup:
    """One group of records read together (a collection, a batch of notes). ``units`` are ready, or ``loader`` returns them when the group is read."""
    group_id: str
    title: str
    units: List[Unit] = field(default_factory=list)
    loader: Optional[Callable[[], Sequence[Unit]]] = None


@dataclass
class GroupResult:
    """What reading one group for one need produced, and how far the reading got."""
    group_id: str
    title: str
    status: str
    reason: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    units_read: int = 0
    units_total: int = 0
    titles: Dict[str, str] = field(default_factory=dict)

    def public(self) -> Dict[str, Any]:
        return {"source_group": self.group_id, "status": self.status, "reason": self.reason,
                "units_read": self.units_read, "units_total": self.units_total}


@dataclass
class DigestResult:
    """The reduced text, the result of every group, and what the run spent: ``analyses`` model calls made, ``reused`` windows served from the cache."""
    text: str
    groups: List[GroupResult]
    analyses: int = 0
    reused: int = 0


@typing.runtime_checkable
class DigestCache(typing.Protocol):
    """Where digests are kept between calls. The key already names the need and the content read, so a host may store the value anywhere durable."""

    def get(self, key: str) -> Optional[Dict[str, Any]]: ...

    def put(self, key: str, value: Dict[str, Any]) -> None: ...


class MemoryDigestCache:
    """The default cache: a dictionary that lives as long as the object, safe to share between threads."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.rows.get(key)

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self.lock:
            self.rows[key] = value
