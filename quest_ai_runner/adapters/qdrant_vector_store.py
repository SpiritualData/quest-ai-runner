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
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import VectorHit, VectorStoreBase

logger = logging.getLogger(__name__)

# Lazy imports: qdrant-client and fastembed are only imported INSIDE the
# constructor so that *importing this module* does not fail when they are
# absent.  Only CONSTRUCTING a QdrantVectorStore fails with a clear hint.


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
        Optional callable ``(texts: List[str]) -> List[List[float]]``.  When
        given it is used for all embedding.  When omitted the store defaults to
        ``fastembed.TextEmbedding`` (model ``BAAI/bge-small-en-v1.5``).
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

        # Resolve the embedder.
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
        """Embed a list of texts; return None on any error."""
        try:
            return self._embed(texts)
        except Exception:
            logger.debug("QdrantVectorStore: embedding failed", exc_info=True)
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
            vecs = self._embed_safe([query])
            if not vecs:
                return []
            coll = self._collection_name(scope)
            self._ensure_collection(coll)
            results = self._client.search(
                collection_name=coll,
                query_vector=vecs[0],
                limit=top_k,
                with_payload=True,
            )
            hits: List[VectorHit] = []
            for r in results:
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
