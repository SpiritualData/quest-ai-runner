"""The vector arm over mounted stores: query the owner's existing points, embed nothing for them.

``StoreFencedVectorStore`` wraps QAR's vector store so the card arm sees exactly the stores this reader may see. The mounts
themselves (``Mount``, ``resolve_mounts``, ``MountedCardRepository``, the path rebase) are the hub's,
``hubofhubs.recall.card_store_mounts``; the vector store they fence, the remote readers (``card_store_vectors``) and the
``QAR_VECTOR_UNATTRIBUTED`` opt-in are QAR's. Whether the reader's vector store is the same space as the owner's is declared
(``vector_space``) rather than guessed; where it is not, the mounted store is served by the keyword arm only.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from hubofhubs.ports.context import VectorHit
from hubofhubs.recall.card_store_identity import EMBEDDED
from hubofhubs.recall.card_store_mounts import MOUNT_ID_SEP, NO_STORE, Mount, Rebase, mounted_card_id, rebase_path

from ..core.adapters_bases import VectorStoreBase
from .card_store_vectors import ReaderFactory, RemoteMountReaders, compatibility, embedded_reason

log = logging.getLogger("quest-ai-runner.card-mounts")


class StoreFencedVectorStore(VectorStoreBase):
    """Wraps a vector store so the card arm sees exactly the stores this reader may see.

    * ``search`` restricts CARD points to the reader's own ``store_id`` plus the live mounts whose
      ``vector_space`` does not contradict the reader's, and rebases a mounted hit's paths and id to the reader's
      terms. The vector server answers anyone who can reach it, so this filter is the only fence the
      vector arm has: a store the reader did not mount is never returned, and the task associations
      the reader's own runs record are attributed to its own store and visible to nobody else.
    * ``sync``/``upsert`` accept only the reader's own items: an item carrying another store's id is
      dropped, so a reader can never write into a mounted store's points.
    """

    def __init__(self, inner: Any, store: Any, vector_space: str = "",
                 include_unattributed: Optional[bool] = None,
                 reader_factory: Optional[ReaderFactory] = None) -> None:
        self.inner = inner
        self.store = store              # a FileContextStore: own_store_id() / live_mounts()
        self.vector_space = vector_space
        # Opt-in (QAR_VECTOR_UNATTRIBUTED=1) for a single-tenant vector store whose task
        # associations predate the fence and are all this reader's own.
        self.include_unattributed = (
            include_unattributed if include_unattributed is not None
            else (os.getenv("QAR_VECTOR_UNATTRIBUTED") or "").strip().lower() in ("1", "true", "yes"))
        self.unsupported_warned = False
        # Read-only handles on OTHER Qdrant backends that mounted stores publish their points on.
        self.remote = RemoteMountReaders(reader_factory)

    def classify(self, mount: Mount) -> str:
        """Where a mounted store's points are, relative to this reader's own vector store.

        ``same``: queried through this reader's own store with the store fence (the shared-server
        case, and any mount whose owner recorded no location: unchanged behaviour). ``remote``:
        the owner recorded a different server or collection prefix, queried read-only. ``other``:
        the owner's points are in an embedded Qdrant in a directory that is not this reader's.
        """
        rec = mount.vector_location
        mine = getattr(self.inner, "location", None)
        if not rec or mine is None:
            return "same"
        loc = str(rec.get("location") or "")
        if loc == EMBEDDED:
            theirs = str(rec.get("path") or "")
            own_path = str(getattr(self.inner, "path", "") or "")
            if not theirs or (mine == EMBEDDED and own_path
                              and os.path.realpath(theirs) == os.path.realpath(own_path)):
                return "same"
            return "other"
        prefix = str(rec.get("collection_prefix") or "")
        if loc == mine and (not prefix or prefix == getattr(self.inner, "collection_prefix", prefix)):
            return "same"
        return "remote"

    def visible_store_ids(self) -> Tuple[List[str], Dict[str, Tuple[Mount, Rebase]]]:
        own = self.store.own_store_id()
        ids: List[str] = [own] if own else []
        by_id: Dict[str, Tuple[Mount, Rebase]] = {}
        for mount, prefix in self.store.live_mounts():
            by_id[mount.store_id] = (mount, prefix)
            if self.classify(mount) != "same":
                continue    # served by search_remote (or by keyword search alone)
            # Queried unless BOTH sides declare a vector space and they differ (a different server
            # or collection holds the owner's points, so there is nothing to find there).
            if self.vector_space and mount.vector_space and mount.vector_space != self.vector_space:
                continue
            ids.append(mount.store_id)
        return ids, by_id

    def rewrite_hit(self, hit: VectorHit, by_id: Dict[str, Tuple[Mount, Rebase]]) -> VectorHit:
        """A mounted store's hit expressed in the reader's terms (id and paths)."""
        payload = dict(hit.payload or {})
        sid = str(payload.get("store_id") or "")
        if sid and sid in by_id:
            mount, prefix = by_id[sid]
            paths = [p for p in (rebase_path(str(x), prefix)
                                 for x in payload.get("paths") or []) if p]
            payload["paths"] = paths
            payload["mounted_from"] = {"store_id": sid}
            bare = str(hit.id)
            bare = bare[len("card:"):] if bare.startswith("card:") else bare
            bare = bare[len(sid) + 1:] if bare.startswith(sid + MOUNT_ID_SEP) else bare
            return VectorHit(id=f"card:{mounted_card_id(sid, bare)}",
                             score=hit.score, text=hit.text, payload=payload)
        return hit

    def search_remote(self, query: str, by_id: Dict[str, Tuple[Mount, Rebase]],
                      top_k: int) -> List[VectorHit]:
        """Hits from mounts whose points are on another backend. READ-ONLY; never raises.

        The query is embedded with THIS reader's query embedder once, and only when some mount
        needs it. A mount that cannot be queried (embedded elsewhere, incompatible embedder,
        unreachable server) is logged once and left to the keyword arm: nothing is copied or
        embedded locally for it.
        """
        out: List[VectorHit] = []
        qvec: Optional[List[float]] = None
        embedded_query = False
        for mount, _prefix in list(by_id.values()):
            kind = self.classify(mount)
            if kind == "same":
                continue
            rec = mount.vector_location or {}
            sid = mount.store_id
            if kind == "other":
                self.remote.log_once(("other", sid), "mounted store %s: vector arm skipped, %s; its "
                                     "cards are served by keyword search only", sid, embedded_reason(rec))
                continue
            if not embedded_query:
                embedded_query = True
                try:
                    qvec = self.inner.embed_query(query)
                except Exception:  # noqa: BLE001
                    qvec = None
            verdict = compatibility(rec, query_dim=len(qvec) if qvec else None,
                                    reader_embedder=str(getattr(self.inner, "embedder_id", "") or ""))
            if not verdict.ok:
                self.remote.log_once(("incompatible", sid, verdict.reason),
                                     "mounted store %s: vector arm skipped, %s; its cards are served "
                                     "by keyword search only", sid, verdict.reason)
                continue
            loc = str(rec.get("location") or "")
            pfx = str(rec.get("collection_prefix") or "qar_ctx")
            reader = self.remote.reader_for(loc, pfx)
            if reader is None:
                continue
            try:
                hits = reader.query(qvec or [], sid, top_k=top_k)
            except Exception as exc:  # noqa: BLE001
                self.remote.mark_down(loc, pfx, exc)
                continue
            out.extend(self.rewrite_hit(h, by_id) for h in hits)
        return out

    def search(self, query: str, *, scope: Optional[Dict[str, Any]] = None,
               top_k: int = 8) -> List[VectorHit]:
        try:
            ids, by_id = self.visible_store_ids()
            if not ids:
                if not self.unsupported_warned:
                    log.warning("this store has no store_id and no readable mounts, so the vector "
                                "arm returns no card points; run `context store-info --init`")
                ids = [NO_STORE]  # fenced with nothing mounted: card points of NO store are visible
            try:
                hits = self.inner.search(query, scope=scope, top_k=top_k, store_ids=ids,
                                         own_store_id=self.store.own_store_id() or NO_STORE,
                                         include_unattributed=self.include_unattributed)
            except TypeError:
                if not self.unsupported_warned:
                    self.unsupported_warned = True
                    log.warning("the vector store does not support store_ids filtering; mounted "
                                "stores are served by the keyword arm only")
                hits = self.inner.search(query, scope=scope, top_k=top_k)
            out: List[VectorHit] = [self.rewrite_hit(h, by_id) for h in hits]
            remote_hits = self.search_remote(query, by_id, top_k)
            if remote_hits:
                # One ranking across both backends: scores are comparable because the embedder
                # was checked compatible with the one that produced the remote vectors.
                out = sorted(out + remote_hits, key=lambda h: h.score, reverse=True)[:max(top_k, 1)]
            return out
        except Exception:  # noqa: BLE001
            log.debug("StoreFencedVectorStore.search failed", exc_info=True)
            return []

    def own_only(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        own = self.store.own_store_id()
        kept = []
        for item in items or []:
            sid = str((item.get("payload") or {}).get("store_id") or "")
            if sid and own and sid != own:
                log.warning("refused to write vector item %s for store %s from this reader",
                            item.get("id"), sid)
                continue
            kept.append(item)
        return kept

    def attributed(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """``own_only`` items, each stamped with this reader's store id when it has none.

        What a reader produces (a task association, a card embedding) is attributed to the reader's
        own store, which is what keeps it out of every other store's fenced searches.
        """
        own = self.store.own_store_id()
        out = []
        for item in self.own_only(items):
            payload = dict(item.get("payload") or {})
            if own and not payload.get("store_id"):
                payload["store_id"] = own
            out.append({**item, "payload": payload})
        return out

    def upsert(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> None:
        self.inner.upsert(self.attributed(items), scope=scope)

    def sync(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> int:
        return self.inner.sync(self.attributed(items), scope=scope)

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        return self.inner.count(scope=scope)

    def evict_oldest(self, n: int, *, scope: Optional[Dict[str, Any]] = None, ts_key: str = "ts") -> int:
        return self.inner.evict_oldest(n, scope=scope, ts_key=ts_key)
