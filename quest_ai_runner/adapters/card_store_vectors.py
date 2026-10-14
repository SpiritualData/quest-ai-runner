"""Read-only access to the points of a mounted store that live on ANOTHER Qdrant backend.

THE CASE
--------
A reader lane keeps its own vectors in a private (embedded) Qdrant that only its OS user can read.
A store it mounts publishes its points on a Qdrant server that both the reader and the owner can
reach. The reader must be able to QUERY those points, and must never WRITE anything to that server
(its own cards, turns, task associations), because other users can reach it.

THE MODEL
---------
* The OWNER records where its vectors live in its identity (``vector_location``:
  ``card_store_identity.vector_location_record``). A reader reads it from the mount, so there is no
  second copy of that configuration on the reader's side.
* ``ReadOnlyVectorReader`` is the reader's only handle on that server. It has no upsert, delete,
  set_payload or create method at all: the no-write guarantee is structural, not a convention, and
  the tests assert it by recording every call the underlying client receives.
* The query is embedded by the reader's OWN query embedder. That is only meaningful when the
  embedder is compatible with the one that produced the stored vectors, so ``compatibility`` checks
  the recorded vector size and (when both sides name one) the embedder family before any query is
  sent. Incompatible or unavailable means: that mount's vector arm is skipped with a log line and
  its cards are served by the keyword arm only. Nothing is ever copied or embedded locally.
* ``embedded`` locations are never opened. An embedded Qdrant takes a write lock on its directory
  and cannot be opened without writing to it, which a reader must not do; so the mount is served by
  the keyword arm and the log says whether the directory is even readable by this process.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.adapters import VectorHit
from ..core.vector_scopes import CARD_SEED_SCOPE
from .card_store_identity import EMBEDDED

log = logging.getLogger("quest-ai-runner.card-vectors")

SCOPE_KEY = "_scope"
STORE_KEY = "store_id"
DEFAULT_PREFIX = "qar_ctx"
# After a failed query to a location, leave it alone this long (seconds) so an unreachable server
# does not add a connection timeout to every turn.
RETRY_AFTER_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 5.0


def seed_scope_digest() -> str:
    from .qdrant_vector_store import _scope_hash
    return str(_scope_hash(CARD_SEED_SCOPE))


# --------------------------------------------------------------------------------------------
# Compatibility: can THIS reader's query embedding be compared with the owner's vectors?
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Compatibility:
    ok: bool
    reason: str = ""


def compatibility(record: Dict[str, Any], *, query_dim: Optional[int],
                  reader_embedder: str = "") -> Compatibility:
    """Whether a query embedded here can be scored against the points ``record`` describes.

    Same dimension is required (a different size is a different collection and a different model).
    When BOTH sides name an embedder family it must be the same string; a side that names none is
    judged by dimension alone. ``query_dim`` is the length of the vector the reader's embedder
    actually produced, ``None`` when it produced none.
    """
    if not query_dim:
        return Compatibility(False, "this reader has no query embedder available")
    want = record.get("vector_size")
    try:
        want_dim = int(want) if want else None
    except (TypeError, ValueError):
        want_dim = None
    if want_dim and want_dim != int(query_dim):
        return Compatibility(False, f"query embeddings are {query_dim}-dimensional but the store's "
                                    f"vectors are {want_dim}-dimensional")
    theirs = str(record.get("embedder") or "").strip()
    mine = str(reader_embedder or "").strip()
    if theirs and mine and theirs != mine:
        return Compatibility(False, f"the store was embedded with {theirs} but this reader embeds "
                                    f"queries with {mine}")
    return Compatibility(True)


def embedded_reason(record: Dict[str, Any]) -> str:
    """Why an ``embedded`` location is not queried, naming readability when a path is recorded."""
    path = str(record.get("path") or "")
    if path and not os.access(path, os.R_OK | os.X_OK):
        return "its vectors are in an embedded Qdrant in a directory this user cannot read"
    return ("its vectors are in an embedded Qdrant, which takes a write lock on its directory and "
            "cannot be opened read-only; publish them on a server (`context store-info "
            "--vector-location URL`)")


# --------------------------------------------------------------------------------------------
# The read-only handle
# --------------------------------------------------------------------------------------------

class ReadOnlyVectorReader:
    """Queries the card points of one store on a Qdrant backend. It has NO write methods.

    Only these client calls are ever made: ``collection_exists``, ``get_collection``,
    ``query_points``, ``get_collections``. Nothing creates a collection, an index or a point.
    """

    def __init__(self, client: Any, *, collection_prefix: str = DEFAULT_PREFIX) -> None:
        self.read_client = client
        self.collection_prefix = collection_prefix or DEFAULT_PREFIX
        self.resolved: Dict[int, Optional[str]] = {}

    def collection_size(self, name: str) -> Optional[int]:
        try:
            info = self.read_client.get_collection(collection_name=name)
            size = getattr(info.config.params.vectors, "size", None)
            return int(size) if size is not None else None
        except Exception:  # noqa: BLE001
            return None

    def resolve_collection(self, vector_size: int) -> Optional[str]:
        """The collection the owner writes to for this size, by the owner's own naming rule.

        A bare ``<prefix>_default`` collection of the matching size wins (the pre-size-keyed
        layout); otherwise ``<prefix>_default_<size>``. None when neither exists (nothing to read).
        """
        if vector_size in self.resolved:
            return self.resolved[vector_size]
        legacy = f"{self.collection_prefix}_default"
        sized = f"{self.collection_prefix}_default_{vector_size}"
        name: Optional[str] = None
        if self.read_client.collection_exists(legacy) and self.collection_size(legacy) == vector_size:
            name = legacy
        elif self.read_client.collection_exists(sized):
            name = sized
        if name is not None:
            self.resolved[vector_size] = name
        return name

    def detect_sizes(self) -> List[int]:
        """Vector sizes of the collections this prefix has on the server (for ``store-info``)."""
        sizes: List[int] = []
        for coll in self.read_client.get_collections().collections:
            name = str(coll.name)
            if name == f"{self.collection_prefix}_default" or name.startswith(f"{self.collection_prefix}_default_"):
                size = self.collection_size(name)
                if size and size not in sizes:
                    sizes.append(size)
        return sizes

    def query(self, vector: Sequence[float], store_id: str, *, top_k: int = 8) -> List[VectorHit]:
        """The card points of ``store_id`` nearest to ``vector``. Raises on transport errors."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        coll = self.resolve_collection(len(vector))
        if coll is None:
            return []
        flt = Filter(must=[
            FieldCondition(key=SCOPE_KEY, match=MatchValue(value=seed_scope_digest())),
            FieldCondition(key=STORE_KEY, match=MatchValue(value=str(store_id))),
        ])
        response = self.read_client.query_points(collection_name=coll, query=list(vector),
                                                 query_filter=flt, limit=top_k, with_payload=True)
        hits: List[VectorHit] = []
        for r in response.points:
            payload = dict(r.payload) if r.payload else {}
            text = payload.pop("_text", "") or ""
            payload.pop(SCOPE_KEY, None)
            item_id = payload.pop("_id", None) or str(r.id)
            hits.append(VectorHit(id=str(item_id), score=float(r.score), text=text, payload=payload))
        return hits


ReaderFactory = Callable[[str, str], Optional[ReadOnlyVectorReader]]


def default_reader_factory(location: str, collection_prefix: str) -> Optional[ReadOnlyVectorReader]:
    """A read-only reader for a server URL. Constructing a client does not connect."""
    if not location or location == EMBEDDED:
        return None
    from qdrant_client import QdrantClient
    return ReadOnlyVectorReader(QdrantClient(url=location, timeout=int(CONNECT_TIMEOUT_SECONDS)),
                                collection_prefix=collection_prefix)


class RemoteMountReaders:
    """Caches one ``ReadOnlyVectorReader`` per (location, prefix) and backs off after a failure."""

    def __init__(self, factory: Optional[ReaderFactory] = None) -> None:
        self.factory = factory or default_reader_factory
        self.readers: Dict[Tuple[str, str], Optional[ReadOnlyVectorReader]] = {}
        self.down_until: Dict[Tuple[str, str], float] = {}
        self.logged: set = set()

    def log_once(self, key: Any, message: str, *args: Any) -> None:
        if key not in self.logged:
            self.logged.add(key)
            log.warning(message, *args)

    def reader_for(self, location: str, prefix: str) -> Optional[ReadOnlyVectorReader]:
        key = (location, prefix)
        if self.down_until.get(key, 0.0) > time.monotonic():
            return None
        if key not in self.readers:
            try:
                self.readers[key] = self.factory(location, prefix)
            except Exception as exc:  # noqa: BLE001
                self.log_once(("build", key), "vector location %s could not be opened read-only (%s); "
                                              "its mounted cards are served by keyword search only",
                              location, exc.__class__.__name__)
                self.readers[key] = None
        return self.readers[key]

    def mark_down(self, location: str, prefix: str, exc: BaseException) -> None:
        self.down_until[(location, prefix)] = time.monotonic() + RETRY_AFTER_SECONDS
        self.log_once(("down", location, prefix),
                      "vector location %s is unreachable (%s); mounted cards stored there are served "
                      "by keyword search only until it answers", location, exc.__class__.__name__)
