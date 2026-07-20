"""QdrantVectorStore — VectorStoreBase implementation backed by Qdrant.

Install the optional extra to use this class::

    pip install 'quest-ai-runner[qdrant]'

This pulls in ``qdrant-client`` (the Qdrant Python SDK) and ``fastembed`` (a
lightweight ONNX-backed embedder that runs locally with no API key).

LOCAL-FILESYSTEM MODE (default)
--------------------------------
When no ``url`` is given, the store uses an *embedded* Qdrant instance whose
state lives under ``path`` (default ``<cwd>/.quest-context/qdrant``).  No server
process is needed; everything runs in-process.

REMOTE / SELF-HOSTED MODE
--------------------------
Pass ``url`` to connect to a Qdrant server (cloud or self-hosted).  The embedded
``path`` argument is ignored when ``url`` is set.

AUTO-UPDATE via sync()
----------------------
``sync(items)`` fetches the stored ``fingerprint`` for each item id, compares it
to the fingerprint in ``items``, and re-embeds + upserts *only* the items that
changed or are missing.  Unchanged items are not re-embedded.  This is the zero-
management auto-update mechanism: callers call ``sync`` on every run; the index
stays fresh without any separate re-index step.

MULTI-TENANT SCOPING
--------------------
Every operation accepts an optional ``scope`` dict (e.g.
``{org_id: ..., team_id: ..., quest_id: ...}``).  All points of a store live in
ONE collection — ``{collection_prefix}_default_{vector_size}``, keyed on the
embedder's true dimension so differently-embedding stores sharing a server
never collide (a bare legacy ``{collection_prefix}_default`` is reused when its
size matches).  The sorted scope items are hashed to a short digest stored on
each point as the ``_scope`` payload field, and searches filter on it (the same
payload-filter multitenancy model as ``QdrantCardVectorStore``, and the model
Qdrant itself recommends over collection-per-tenant).

Visibility rules:

- Unscoped points (``scope=None``) are SHARED: visible to unscoped searches and
  to every scoped search (shared corpus + scope-private additions).
- Scoped points are visible only to searches carrying the SAME scope.
- Unscoped searches see only unscoped points (never any scope's private data).

Read operations (``search``/``count``/``evict_oldest``) NEVER create a
collection; only writes do.  An earlier version of this store created one
Qdrant collection per unique scope — including on *search* — which sprawled
into hundreds of permanently empty collections (each adding startup shard-
recovery time on the server).  Deployments that ran that version can call
``prune_scope_collections()`` once to delete the empty leftover per-scope
collections.

NEVER-RAISES CONTRACT
---------------------
``search``, ``upsert``, and ``sync`` all catch every exception internally and
return a safe default (``[]`` / ``0``).  The constructor raises ``ImportError``
(clearly hinted) when the required packages are missing.

VOYAGE AI EMBEDDER
------------------
Use ``make_voyage_embedder`` to build a Voyage AI-backed callable.  Pass
separate ``embedder`` (for upsert/sync) and ``query_embedder`` (for search) to
match quest-backend's production setup, which uses ``input_type="document"`` for
stored items and ``input_type="query"`` for search queries::

    from quest_ai_runner.adapters.qdrant_vector_store import (
        QdrantVectorStore, make_voyage_embedder,
    )

    store = QdrantVectorStore(
        url="http://localhost:6333",
        vector_size=1024,
        embedder=make_voyage_embedder(input_type="document"),
        query_embedder=make_voyage_embedder(input_type="query"),
    )

OPENAI EMBEDDER
---------------
Use ``make_openai_embedder`` for OpenAI-backed embeddings (symmetric -- same
callable for both document and query roles)::

    from quest_ai_runner.adapters.qdrant_vector_store import (
        QdrantVectorStore, make_openai_embedder,
    )

    embedder = make_openai_embedder()   # reads OPENAI_API_KEY + QAR_OPENAI_EMBEDDING_MODEL
    store = QdrantVectorStore(
        url="http://localhost:6333",
        vector_size=1536,               # text-embedding-3-small default
        embedder=embedder,
        query_embedder=embedder,        # same callable -- OpenAI embeddings are symmetric
    )
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import VectorHit, VectorStoreBase

logger = logging.getLogger(__name__)

# Lazy imports: qdrant-client and fastembed are only imported INSIDE the
# constructor so that *importing this module* does not fail when they are
# absent.  Only CONSTRUCTING a QdrantVectorStore fails with a clear hint.


def make_voyage_embedder(
    *,
    model: Optional[str] = None,
    input_type: str = "document",
    api_key: Optional[str] = None,
) -> Callable[[List[str]], List[List[float]]]:
    """Build a Voyage AI-backed embedding callable.

    Parameters
    ----------
    model:
        Voyage model name.  Falls back to the ``VOYAGE_MODEL`` env var, then
        ``"voyage-3-lite"``.
    input_type:
        ``"document"`` for items being stored; ``"query"`` for search queries.
        Pass a separate embedder per role so quest-backend's asymmetric
        embedding is reproduced correctly.
    api_key:
        Voyage API key.  When omitted the ``VOYAGE_API_KEY`` env var (or the
        ``voyageai`` library's own default lookup) is used.

    Returns
    -------
    Callable[[List[str]], List[List[float]]]
        A callable that embeds a list of strings and returns their vectors.
        Errors propagate to the caller (``_embed_safe`` in ``QdrantVectorStore``
        swallows them).

    Raises
    ------
    ImportError
        Raised immediately (at factory-call time) when ``voyageai`` is not
        installed, with an install hint.
    """
    try:
        import voyageai  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "make_voyage_embedder requires the voyageai package. "
            "Install with: pip install voyageai"
        ) from exc

    _model = model or os.getenv("VOYAGE_MODEL", "voyage-3-lite")
    _input_type = input_type
    _api_key = api_key

    def _embed(texts: List[str]) -> List[List[float]]:
        import voyageai as _va
        client = _va.Client(api_key=_api_key)
        return client.embed(texts, model=_model, input_type=_input_type).embeddings

    return _embed


def make_openai_embedder(
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Callable[[List[str]], List[List[float]]]:
    """Build an OpenAI-backed embedding callable.

    Parameters
    ----------
    model:
        OpenAI embedding model name.  Falls back to the ``QAR_OPENAI_EMBEDDING_MODEL``
        env var, then ``"text-embedding-3-small"``.
    api_key:
        OpenAI API key.  When omitted the ``OPENAI_API_KEY`` env var is used.

    Returns
    -------
    Callable[[List[str]], List[List[float]]]
        A callable that embeds a list of strings and returns their vectors.
        OpenAI embeddings are symmetric (no ``input_type`` distinction), so the
        same callable can be passed as both ``embedder`` and ``query_embedder``.

    Raises
    ------
    ImportError
        Raised immediately (at factory-call time) when the ``openai`` package is
        not installed, with an install hint.
    """
    try:
        import openai  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "make_openai_embedder requires the openai package. "
            "Install with: pip install openai"
        ) from exc

    _model = model or os.getenv("QAR_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    _api_key = api_key or os.getenv("OPENAI_API_KEY")

    def _embed(texts: List[str]) -> List[List[float]]:
        import openai as _oa
        client = _oa.OpenAI(api_key=_api_key)
        response = client.embeddings.create(input=texts, model=_model)
        return [item.embedding for item in response.data]

    return _embed


# Payload field carrying a point's scope digest.  Absent on unscoped (shared)
# points.  Matches the underscore-prefixed reserved-field style of ``_text`` /
# ``_fingerprint`` below.
_SCOPE_KEY = "_scope"


def _scope_hash(scope: Optional[Dict[str, Any]]) -> Optional[str]:
    """Derive a stable short digest identifying a scope dict.

    Returns ``None`` for an empty/absent scope.  Otherwise the sorted
    (key, value) pairs are hashed to a short hex digest — the same digest the
    legacy collection-per-scope layout used as its collection-name suffix.
    """
    if not scope:
        return None
    parts = sorted(f"{k}={v}" for k, v in scope.items())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _point_id(item_id: Any, scope_digest: Optional[str]) -> int:
    """Deterministic numeric Qdrant point id for an item id within a scope.

    Scoped ids are namespaced by the scope digest so the same item id in two
    different scopes never collides now that all scopes share one collection.
    Unscoped ids use the bare item id — unchanged from the legacy layout, so
    existing unscoped points keep their identity (fingerprint-based ``sync``
    still recognizes them).
    """
    ns = f"{scope_digest}|{item_id}" if scope_digest else str(item_id)
    return int(hashlib.sha256(ns.encode()).hexdigest()[:15], 16)


class QdrantVectorStore(VectorStoreBase):
    """VectorStore backed by a local-filesystem or remote Qdrant instance.

    Parameters
    ----------
    path:
        Filesystem path for the embedded local Qdrant DB (used when ``url`` is
        not given).  Defaults to ``<cwd>/.quest-context/qdrant``.
    url:
        If given, connect to this Qdrant server URL instead of using embedded mode.
    collection_prefix:
        All points of this store live in the single collection
        ``{collection_prefix}_default_{vector_size}`` (scope is a payload
        filter, not a collection suffix — see MULTI-TENANT SCOPING in the
        module docstring).  Default: ``"qar_ctx"``.
    embedder:
        Optional callable ``(texts: List[str]) -> List[List[float]]``.  Used for
        ``upsert`` and ``sync`` (items being stored).  When omitted the store
        defaults to ``fastembed.TextEmbedding`` (model ``BAAI/bge-small-en-v1.5``).
    query_embedder:
        Optional callable for embedding search queries in ``search``.  When not
        given, falls back to ``embedder`` so single-embedder callers are unchanged.
        Pass a distinct callable here when the embedder distinguishes document vs
        query input types (e.g. Voyage AI ``input_type="document"`` /
        ``"query"``).
    vector_size:
        Expected dimensionality of the embedding vectors.  Default: 384
        (matches the fastembed default model).  This is only the initial
        expectation: the store adopts the REAL dimension observed from the
        embedder's output before any collection is created, so a misdeclared
        size never pins a collection to the wrong dimension (which would make
        Qdrant silently decline every write).
    """

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        url: Optional[str] = None,
        collection_prefix: str = "qar_ctx",
        embedder: Optional[Callable[[List[str]], List[List[float]]]] = None,
        query_embedder: Optional[Callable[[List[str]], List[List[float]]]] = None,
        vector_size: int = 384,
    ) -> None:
        # --- Lazy imports with a clear install hint --------------------------
        try:
            from qdrant_client import QdrantClient  # noqa: F401
            from qdrant_client.models import (  # noqa: F401
                Distance,
                PointStruct,
                VectorParams,
            )
        except ImportError as exc:
            raise ImportError(
                "QdrantVectorStore requires qdrant-client and fastembed. "
                "Install with: pip install 'quest-ai-runner[qdrant]'"
            ) from exc

        self._prefix = collection_prefix
        self._vector_size = vector_size
        # Resolved shared-collection name (see _resolve_collection).  None until
        # first resolved; reset whenever the effective vector size changes.
        self._resolved_collection: Optional[str] = None
        self._size_mismatch_warned = False

        # Build the client (embedded or remote).
        if url:
            self._client = QdrantClient(url=url)
            self._server_mode = True
        else:
            _path = path or ".quest-context/qdrant"
            self._client = QdrantClient(path=_path)
            self._server_mode = False

        # Resolve the document embedder (used by upsert/sync).
        if embedder is not None:
            self._embed = embedder
        else:
            try:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

                def _fastembed(texts: List[str]) -> List[List[float]]:
                    return [list(v) for v in _model.embed(texts)]

                self._embed = _fastembed
            except ImportError as exc:
                raise ImportError(
                    "QdrantVectorStore requires fastembed for default embedding. "
                    "Install with: pip install 'quest-ai-runner[qdrant]'"
                ) from exc

        # Resolve the query embedder (used by search).
        # Falls back to the document embedder when not given so that single-
        # embedder callers are completely unchanged.
        self._query_embed: Callable[[List[str]], List[List[float]]] = (
            query_embedder if query_embedder is not None else self._embed
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _adopt_embedding_dim(self, dim: int) -> None:
        """Adopt the REAL embedding dimension observed from the embedder.

        ``vector_size`` is only the constructor's expectation; consumers wire
        embedders (Voyage 1024-d, OpenAI 1536-d, fastembed 384-d) without
        always updating it.  Creating a collection at the declared size and
        then upserting differently-sized vectors makes Qdrant decline every
        point server-side while the never-raises contract keeps the caller
        oblivious — a silent context loss.  Adopting the observed dimension
        BEFORE any collection is resolved/created kills that class of bug: the
        shared collection is always created at the embedder's true size.
        """
        if dim and dim != self._vector_size:
            logger.warning(
                "QdrantVectorStore: embedder produced %d-dim vectors but "
                "vector_size=%d was configured; adopting %d",
                dim, self._vector_size, dim,
            )
            self._vector_size = dim
            self._resolved_collection = None

    def _collection_name(self) -> str:
        """The single collection ALL points of this store live in, regardless of scope.

        Scope is a payload filter (``_SCOPE_KEY``), not a collection-name
        suffix.  The name is keyed on the vector size —
        ``{prefix}_default_{size}`` — so two stores with different embedder
        configurations pointing at the SAME shared server never collide on one
        collection (mixed vector sizes in one collection make Qdrant decline
        the mismatched writes point-by-point, silently under the never-raises
        contract).

        Legacy compatibility: when the bare ``{prefix}_default`` collection
        (the pre-size-keyed layout) exists AND its configured vector size
        matches, it is reused so existing data needs no migration.  When it
        exists with a DIFFERENT size, a warning is logged once and the
        size-keyed name is used instead — writes land somewhere real rather
        than being declined against the mismatched collection.
        """
        if self._resolved_collection is not None:
            return self._resolved_collection
        legacy = f"{self._prefix}_default"
        sized = f"{self._prefix}_default_{self._vector_size}"
        try:
            exists = bool(self._client.collection_exists(legacy))
        except Exception:  # noqa: BLE001 — transport trouble: fall back, don't cache
            logger.debug("QdrantVectorStore: legacy collection check failed", exc_info=True)
            return sized
        name = sized
        if exists:
            legacy_size = self._collection_vector_size(legacy)
            if legacy_size == self._vector_size:
                name = legacy
            elif legacy_size is not None and not self._size_mismatch_warned:
                logger.warning(
                    "QdrantVectorStore: existing collection %s holds %s-dim vectors "
                    "but this store embeds %s-dim; using %s instead",
                    legacy, legacy_size, self._vector_size, sized,
                )
                self._size_mismatch_warned = True
        self._resolved_collection = name
        return name

    def _collection_vector_size(self, name: str) -> Optional[int]:
        """The configured (unnamed single-)vector size of a collection, or None."""
        try:
            info = self._client.get_collection(collection_name=name)
            vectors = info.config.params.vectors
            size = getattr(vectors, "size", None)
            return int(size) if size is not None else None
        except Exception:  # noqa: BLE001
            logger.debug("QdrantVectorStore: could not read %s vector size", name, exc_info=True)
            return None

    def _ensure_collection(self, name: str) -> None:
        """Create the Qdrant collection if it does not exist yet.  WRITE paths only.

        Read paths (``search``/``count``/``evict_oldest``) must never call this:
        creating collections on read is how the legacy collection-per-scope
        layout sprawled into hundreds of empty collections.
        """
        from qdrant_client.models import Distance, VectorParams

        existing = {c.name for c in self._client.get_collections().collections}
        if name not in existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=self._vector_size,
                    distance=Distance.COSINE,
                ),
            )
        # Keyword index on the scope field so scoped filters stay fast as the
        # collection grows.  Server mode only (the embedded local engine has no
        # payload indexes and warns; filtering works without one).  Idempotent,
        # best-effort.
        if self._server_mode:
            try:
                self._client.create_payload_index(
                    collection_name=name,
                    field_name=_SCOPE_KEY,
                    field_schema="keyword",
                )
            except Exception:  # noqa: BLE001
                logger.debug("QdrantVectorStore: payload index creation skipped", exc_info=True)

    def _visibility_filter(self, scope: Optional[Dict[str, Any]]) -> Any:
        """Filter for SEARCH visibility under ``scope``.

        Scoped search: points carrying this scope's digest OR shared (unscoped)
        points.  Unscoped search: shared points only — no scope's private data.
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            IsEmptyCondition,
            MatchValue,
            PayloadField,
        )

        shared = IsEmptyCondition(is_empty=PayloadField(key=_SCOPE_KEY))
        digest = _scope_hash(scope)
        if digest is None:
            return Filter(must=[shared])
        return Filter(
            should=[
                FieldCondition(key=_SCOPE_KEY, match=MatchValue(value=digest)),
                shared,
            ]
        )

    def _exact_scope_filter(self, scope: Optional[Dict[str, Any]]) -> Any:
        """Filter matching ONLY points belonging to exactly ``scope``.

        Used by ``count``/``evict_oldest`` so capacity accounting and eviction
        stay per-scope (shared points are counted/evicted only by unscoped
        callers, as under the legacy one-collection-per-scope layout).
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            IsEmptyCondition,
            MatchValue,
            PayloadField,
        )

        digest = _scope_hash(scope)
        if digest is None:
            return Filter(must=[IsEmptyCondition(is_empty=PayloadField(key=_SCOPE_KEY))])
        return Filter(must=[FieldCondition(key=_SCOPE_KEY, match=MatchValue(value=digest))])

    def _embed_safe(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Embed a list of texts (document path); return None on any error."""
        try:
            return self._embed(texts)
        except Exception:
            logger.debug("QdrantVectorStore: embedding failed", exc_info=True)
            return None

    def _query_embed_safe(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Embed a list of query texts; return None on any error."""
        try:
            return self._query_embed(texts)
        except Exception:
            logger.debug("QdrantVectorStore: query embedding failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # VectorStoreBase implementation
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        top_k: int = 8,
    ) -> List[VectorHit]:
        """Embed ``query`` and return the top-``top_k`` nearest hits.  Never raises.

        Never creates a collection: when nothing has been written yet the
        search simply returns ``[]``.
        """
        try:
            vecs = self._query_embed_safe([query])
            if not vecs:
                return []
            self._adopt_embedding_dim(len(vecs[0]))
            coll = self._collection_name()
            # Use query_points (the current Qdrant API; the old .search() was removed).
            response = self._client.query_points(
                collection_name=coll,
                query=vecs[0],
                query_filter=self._visibility_filter(scope),
                limit=top_k,
                with_payload=True,
            )
            hits: List[VectorHit] = []
            for r in response.points:
                payload = dict(r.payload) if r.payload else {}
                text = payload.pop("_text", "") or ""
                payload.pop(_SCOPE_KEY, None)
                # Prefer the original item id stored at upsert; points written
                # before the ``_id`` field existed fall back to the numeric id.
                item_id = payload.pop("_id", None) or str(r.id)
                hits.append(
                    VectorHit(
                        id=str(item_id),
                        score=float(r.score),
                        text=text,
                        payload=payload,
                    )
                )
            return hits
        except Exception:
            logger.debug("QdrantVectorStore.search failed", exc_info=True)
            return []

    def upsert(
        self,
        items: List[Dict[str, Any]],
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Embed item texts and upsert into the scope collection.  Never raises."""
        try:
            if not items:
                return
            from qdrant_client.models import PointStruct

            texts = [item.get("text", "") or "" for item in items]
            vecs = self._embed_safe(texts)
            if not vecs or len(vecs) != len(items):
                return
            self._adopt_embedding_dim(len(vecs[0]))
            coll = self._collection_name()
            self._ensure_collection(coll)
            digest = _scope_hash(scope)
            points: List[PointStruct] = []
            for item, vec in zip(items, vecs):
                item_id = item["id"]
                payload = dict(item.get("payload") or {})
                fp = item.get("fingerprint")
                if fp is not None:
                    payload["_fingerprint"] = fp
                payload["_text"] = item.get("text", "") or ""
                # Preserve the caller's item id so search hits carry it back
                # (the numeric point id is a hash, meaningless to consumers).
                payload["_id"] = str(item_id)
                if digest is not None:
                    payload[_SCOPE_KEY] = digest
                # Qdrant point ids must be unsigned int or uuid string; hash the
                # (scope-namespaced) string id to a deterministic integer.
                points.append(
                    PointStruct(id=_point_id(item_id, digest), vector=vec, payload=payload)
                )
            self._client.upsert(collection_name=coll, points=points)
        except Exception:
            logger.debug("QdrantVectorStore.upsert failed", exc_info=True)

    def sync(
        self,
        items: List[Dict[str, Any]],
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> int:
        """AUTO-UPDATE: re-embed only changed/new items.  Returns count re-embedded.  Never raises."""
        try:
            if not items:
                return 0
            coll = self._collection_name()
            self._ensure_collection(coll)
            digest = _scope_hash(scope)

            # Fetch stored points to compare fingerprints.
            numeric_ids = [_point_id(i["id"], digest) for i in items]
            try:
                stored_points = self._client.retrieve(
                    collection_name=coll,
                    ids=numeric_ids,
                    with_payload=True,
                )
            except Exception:
                stored_points = []

            # Map numeric_id -> stored fingerprint.
            stored_fps: Dict[int, Any] = {}
            for sp in stored_points:
                fp_val = (sp.payload or {}).get("_fingerprint")
                stored_fps[sp.id] = fp_val

            # Determine which items need re-embedding.
            to_upsert: List[Dict[str, Any]] = []
            for item in items:
                nid = _point_id(item["id"], digest)
                new_fp = item.get("fingerprint")
                old_fp = stored_fps.get(nid)
                if nid not in stored_fps or new_fp != old_fp:
                    to_upsert.append(item)

            if not to_upsert:
                return 0

            self.upsert(to_upsert, scope=scope)
            return len(to_upsert)
        except Exception:
            logger.debug("QdrantVectorStore.sync failed", exc_info=True)
            return 0

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        """Return the number of stored points belonging to exactly ``scope``.

        Shared (unscoped) points are counted only when ``scope`` is None, so
        per-scope capacity accounting matches the eviction filter.  Never
        raises, and never creates a collection (missing collection -> 0).
        """
        try:
            result = self._client.count(
                collection_name=self._collection_name(),
                count_filter=self._exact_scope_filter(scope),
                exact=True,
            )
            return int(result.count or 0)
        except Exception:
            logger.debug("QdrantVectorStore.count failed", exc_info=True)
            return 0

    def evict_oldest(
        self,
        n: int,
        *,
        scope: Optional[Dict[str, Any]] = None,
        ts_key: str = "ts",
    ) -> int:
        """Delete the ``n`` oldest points of exactly ``scope`` (by ``ts_key``, asc).

        Uses scroll (filtered to the exact scope, so one scope's eviction never
        deletes another scope's or the shared points), sorts by the ``ts_key``
        field (ascending; missing ts treated as 0), then deletes the oldest
        ``n`` via client.delete.  Returns the count actually deleted.  Never
        raises, and never creates a collection.
        """
        try:
            if n <= 0:
                return 0
            from qdrant_client.models import PointIdsList

            coll = self._collection_name()

            # Scroll through the scope's points to collect (numeric_id, ts).
            all_points: List[tuple] = []  # list of (ts_value, numeric_id)
            offset = None
            while True:
                scroll_result = self._client.scroll(
                    collection_name=coll,
                    scroll_filter=self._exact_scope_filter(scope),
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                batch, next_offset = scroll_result
                for pt in batch:
                    ts_val = (pt.payload or {}).get(ts_key, 0) or 0
                    all_points.append((float(ts_val), pt.id))
                if next_offset is None:
                    break
                offset = next_offset

            if not all_points:
                return 0

            # Sort ascending by ts (oldest first).
            all_points.sort(key=lambda x: x[0])
            to_delete = [pt_id for _, pt_id in all_points[:n]]

            if not to_delete:
                return 0

            self._client.delete(
                collection_name=coll,
                points_selector=PointIdsList(points=to_delete),
            )
            return len(to_delete)
        except Exception:
            logger.debug("QdrantVectorStore.evict_oldest failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_scope_collections(self, *, pace_seconds: float = 0.0) -> int:
        """Delete EMPTY legacy per-scope collections left by the old layout.

        An earlier version of this store created one collection per unique
        scope (``{prefix}_<digest>``) — including on mere *search* — so a
        long-lived shared server accumulates hundreds of empty collections,
        each adding shard-recovery time to server startup.  This deletes every
        ``{prefix}_``-prefixed collection that holds ZERO points, except the
        unified collections (``{prefix}_default`` and any size-keyed
        ``{prefix}_default_*``).  Non-empty legacy collections are left
        untouched (logged at warning level) — by construction of the old code
        they should not exist, so one deserves a human look.

        ``pace_seconds`` sleeps between deletions — pass a small value (e.g.
        0.25) on a busy shared server so a long sweep does not starve its
        other clients.

        Returns the number of collections deleted.  Never raises.  Safe to run
        repeatedly; call it once per deployment after upgrading.
        """
        import time as _time

        deleted = 0
        try:
            keep_prefix = f"{self._prefix}_default"
            names = [c.name for c in self._client.get_collections().collections]
            for name in names:
                if not name.startswith(f"{self._prefix}_") or name.startswith(keep_prefix):
                    continue
                try:
                    info = self._client.get_collection(collection_name=name)
                    if int(info.points_count or 0) > 0:
                        logger.warning(
                            "QdrantVectorStore.prune_scope_collections: legacy scope "
                            "collection %s is non-empty (%s points); leaving it in place",
                            name, info.points_count,
                        )
                        continue
                    self._client.delete_collection(collection_name=name)
                    deleted += 1
                    if pace_seconds > 0:
                        _time.sleep(pace_seconds)
                except Exception:  # noqa: BLE001 — one bad collection must not stop the sweep
                    logger.debug(
                        "QdrantVectorStore.prune_scope_collections: skipping %s",
                        name, exc_info=True,
                    )
            return deleted
        except Exception:
            logger.debug("QdrantVectorStore.prune_scope_collections failed", exc_info=True)
            return deleted
