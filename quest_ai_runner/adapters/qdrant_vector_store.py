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
``{org_id: ..., team_id: ..., quest_id: ...}``).  The store hashes the sorted
scope items to derive a collection-name suffix, creating one Qdrant collection
per unique scope.  When ``scope`` is None the default collection is used.

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


def _scope_suffix(scope: Optional[Dict[str, Any]], prefix: str) -> str:
    """Derive a stable collection name from a scope dict + prefix.

    When ``scope`` is None or empty the suffix is ``"default"``.  Otherwise
    the sorted (key, value) pairs are hashed to a short hex digest so that
    distinct scopes map to distinct collections and the name stays within
    Qdrant's collection-name limits.
    """
    if not scope:
        return f"{prefix}_default"
    parts = sorted(f"{k}={v}" for k, v in scope.items())
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


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
        All collections created by this store are named
        ``{collection_prefix}_{scope-suffix}``.  Default: ``"qar_ctx"``.
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
        Dimensionality of the embedding vectors.  Must match whatever ``embedder``
        produces.  Default: 384 (matches the fastembed default model).
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

        # Build the client (embedded or remote).
        if url:
            self._client = QdrantClient(url=url)
        else:
            _path = path or ".quest-context/qdrant"
            self._client = QdrantClient(path=_path)

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

    def _collection_name(self, scope: Optional[Dict[str, Any]]) -> str:
        return _scope_suffix(scope, self._prefix)

    def _ensure_collection(self, name: str) -> None:
        """Create the Qdrant collection if it does not exist yet."""
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
        """Embed ``query`` and return the top-``top_k`` nearest hits.  Never raises."""
        try:
            vecs = self._query_embed_safe([query])
            if not vecs:
                return []
            coll = self._collection_name(scope)
            self._ensure_collection(coll)
            # Use query_points (the current Qdrant API; the old .search() was removed).
            response = self._client.query_points(
                collection_name=coll,
                query=vecs[0],
                limit=top_k,
                with_payload=True,
            )
            hits: List[VectorHit] = []
            for r in response.points:
                payload = dict(r.payload) if r.payload else {}
                text = payload.pop("_text", "") or ""
                hits.append(
                    VectorHit(
                        id=str(r.id),
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
            coll = self._collection_name(scope)
            self._ensure_collection(coll)
            points: List[PointStruct] = []
            for item, vec in zip(items, vecs):
                item_id = item["id"]
                payload = dict(item.get("payload") or {})
                fp = item.get("fingerprint")
                if fp is not None:
                    payload["_fingerprint"] = fp
                payload["_text"] = item.get("text", "") or ""
                # Qdrant point ids must be unsigned int or uuid string; hash the
                # string id to a deterministic integer.
                numeric_id = int(
                    hashlib.sha256(str(item_id).encode()).hexdigest()[:15], 16
                )
                points.append(
                    PointStruct(id=numeric_id, vector=vec, payload=payload)
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
            coll = self._collection_name(scope)
            self._ensure_collection(coll)

            # Build a map of item-id -> item for fast lookup.
            item_map: Dict[str, Dict[str, Any]] = {str(i["id"]): i for i in items}

            # Fetch stored points to compare fingerprints.
            numeric_ids = [
                int(hashlib.sha256(str(i["id"]).encode()).hexdigest()[:15], 16)
                for i in items
            ]
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
                nid = int(
                    hashlib.sha256(str(item["id"]).encode()).hexdigest()[:15], 16
                )
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
        """Return the number of stored points in the scope collection.  Never raises."""
        try:
            coll = self._collection_name(scope)
            self._ensure_collection(coll)
            info = self._client.get_collection(collection_name=coll)
            return int(info.points_count or 0)
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
        """Delete the ``n`` oldest points (sorted by ``ts_key`` payload field, asc).

        Uses scroll to list all points with payload, sorts by the ``ts_key`` field
        (ascending; missing ts treated as 0), then deletes the oldest ``n`` via
        client.delete.  Returns the count actually deleted.  Never raises.
        """
        try:
            if n <= 0:
                return 0
            from qdrant_client.models import PointIdsList

            coll = self._collection_name(scope)
            self._ensure_collection(coll)

            # Scroll through ALL points to collect (numeric_id, ts).
            all_points: List[tuple] = []  # list of (ts_value, numeric_id)
            offset = None
            while True:
                scroll_result = self._client.scroll(
                    collection_name=coll,
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
