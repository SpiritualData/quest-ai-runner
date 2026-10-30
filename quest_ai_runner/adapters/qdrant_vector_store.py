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

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from esoi import implementation
from hubofhubs.ports.context import VectorHit
from ..core.adapters_bases import VectorStoreBase
from ..core.vector_scopes import CARD_SEED_SCOPE
from .qdrant_embedders import embed_batch_size, embed_max_chars, FASTEMBED_EMBEDDER_ID

logger = logging.getLogger(__name__)

# Lazy imports: qdrant-client and fastembed are only imported INSIDE the
# constructor so that *importing this module* does not fail when they are
# absent.  Only CONSTRUCTING a QdrantVectorStore fails with a clear hint.

# Payload field carrying a point's scope digest.  Absent on unscoped (shared)
# points.  Matches the underscore-prefixed reserved-field style of ``_text`` /
# ``_fingerprint`` below.
_SCOPE_KEY = "_scope"

# Payload field naming the card store a point was embedded for (see card_store_identity). Card
# points carry it so a reader can query a mounted store's EXISTING points by id instead of
# embedding its own copy. Points without it (task associations, points not yet tagged) are
# store-agnostic and stay visible to every search.
_STORE_KEY = "store_id"


@dataclass(frozen=True)
class PointMove:
    """One card point to relabel for a store merge (see ``relabel_points``)."""
    old_item_id: str
    new_item_id: str
    store_id: str
    paths: Tuple[str, ...]


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


@implementation("index-by-meaning", "qdrant")
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
        embedder_id: Optional[str] = None,
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
        # Where this store's points live and which model made them, as recorded in a store's
        # ``vector_location`` identity (card_store_identity). ``embedder_id`` names the model
        # family; "" means unknown (a caller-supplied embedder that did not say).
        self.location = url or "embedded"
        self.collection_prefix = collection_prefix
        self.embedder_id = embedder_id if embedder_id is not None else ("" if embedder is not None
                                                                          else FASTEMBED_EMBEDDER_ID)
        # Resolved shared-collection name (see _resolve_collection).  None until
        # first resolved; reset whenever the effective vector size changes.
        self._resolved_collection: Optional[str] = None
        self._size_mismatch_warned = False

        # Build the client (embedded or remote).
        if url:
            self._client = QdrantClient(url=url)
            self._server_mode = True
            self.path = None
        else:
            _path = path or ".quest-context/qdrant"
            self._client = QdrantClient(path=_path)
            self._server_mode = False
            self.path = os.path.realpath(_path)

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
            try:
                self._client.create_payload_index(
                    collection_name=name,
                    field_name=_STORE_KEY,
                    field_schema="keyword",
                )
            except Exception:  # noqa: BLE001
                logger.debug("QdrantVectorStore: store index creation skipped", exc_info=True)

    def _store_filter(self, store_ids: Sequence[str], own_store_id: Optional[str],
                      include_unattributed: bool) -> Any:
        """The store fence: which attributed points a mount-aware reader may see.

        * CARD points (the seeded-card partition) are admitted for exactly ``store_ids``: the
          reader's own store plus the stores it mounts.
        * Everything else (task associations the reader's own runs recorded) is admitted for the
          reader's OWN store only. A mounted store's associations are its owner's task memory and
          are not part of what a mount shares.
        * A point with no ``store_id`` is attributed to nobody and is NOT served, unless the caller
          opts in with ``include_unattributed`` for NON-card points (a single-tenant store whose
          pre-fence associations are all the reader's own). Cards are never served unattributed.

        This filter is the only fence the vector arm has: the server answers anyone who can reach it.
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            IsEmptyCondition,
            MatchAny,
            MatchValue,
            PayloadField,
        )

        seed_in = FieldCondition(key=_SCOPE_KEY, match=MatchValue(value=_scope_hash(CARD_SEED_SCOPE)))
        branches: List[Any] = [
            Filter(must=[seed_in,
                         FieldCondition(key=_STORE_KEY, match=MatchAny(any=[str(i) for i in store_ids]))]),
            Filter(must=[FieldCondition(key=_STORE_KEY, match=MatchValue(value=str(own_store_id or "")))],
                   must_not=[seed_in]),
        ]
        if include_unattributed:
            branches.append(Filter(must=[IsEmptyCondition(is_empty=PayloadField(key=_STORE_KEY))],
                                   must_not=[seed_in]))
        return Filter(should=branches)

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
        seed_digest = _scope_hash(CARD_SEED_SCOPE)
        # The seeded-card partition is visibility-shared, like an unscoped point: a card belongs
        # to the corpus, not to one task's scope. What its own scope buys is that the capacity
        # accounting in ``count``/``evict_oldest`` (which use the EXACT filter below) can never
        # reach it. See core/vector_scopes.py.
        seeded = FieldCondition(key=_SCOPE_KEY, match=MatchValue(value=seed_digest))
        digest = _scope_hash(scope)
        if digest is None or digest == seed_digest:
            return Filter(should=[shared, seeded])
        return Filter(
            should=[
                FieldCondition(key=_SCOPE_KEY, match=MatchValue(value=digest)),
                shared,
                seeded,
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
        """Embed a list of query texts; return None on any error.

        The most recent result is remembered: a mount-aware reader asks for the same query's
        embedding once for this store and once for each mount on another backend, and an embedding
        call can be a paid network request.
        """
        key = tuple(texts)
        memo = getattr(self, "query_memo", None)
        if memo is not None and memo[0] == key:
            return memo[1]
        try:
            vecs = self._query_embed(texts)
            self.query_memo = (key, vecs)
            return vecs
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
        store_ids: Optional[Sequence[str]] = None,
        own_store_id: Optional[str] = None,
        include_unattributed: bool = False,
    ) -> List[VectorHit]:
        """Embed ``query`` and return the top-``top_k`` nearest hits.  Never raises.

        ``store_ids`` (optional) switches on the STORE FENCE (see ``_store_filter``): card points
        are admitted only for the named stores (the reader's own plus any it mounts) and other
        points only for ``own_store_id``. ``None`` is the unfiltered behaviour every existing
        caller has.

        Never creates a collection: when nothing has been written yet the
        search simply returns ``[]``.
        """
        try:
            vecs = self._query_embed_safe([query])
            if not vecs:
                return []
            self._adopt_embedding_dim(len(vecs[0]))
            coll = self._collection_name()
            from qdrant_client.models import Filter as _Filter
            visible = self._visibility_filter(scope)
            if store_ids:
                visible = _Filter(must=[visible, self._store_filter(
                    store_ids, own_store_id, include_unattributed)])
            # Use query_points (the current Qdrant API; the old .search() was removed).
            response = self._client.query_points(
                collection_name=coll,
                query=vecs[0],
                query_filter=visible,
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
        """Embed item texts and upsert into the scope collection.  Never raises.

        Embeds in BOUNDED BATCHES rather than one call per corpus, because the cost of an
        embedding call is set by the batch handed to it, not by the number of calls. The embedder
        is a local ONNX model (fastembed by default): its working set scales with how many texts
        are in flight at once, and it does not shrink back between them.

        INCIDENT (2026-09-12). ``sync()`` passes every new or changed item in a single call, which
        on a cold store is the ENTIRE corpus. A seeding pass over ~5,700 cards took the runner from
        ~40MB to 5.5GB in well under a minute -- reproduced three times, each caught in the act
        with the same stack (``_maybe_seed`` -> ``sync`` -> ``upsert`` -> fastembed -> onnxruntime)
        -- and the kernel OOM-killed the process mid-task. Because the seed runs inside the vector
        ARM of context assembly, the visible symptom was the arm "missing the assembly deadline",
        which reads like slowness rather than the memory event it actually was. And because the
        runner was killed rather than raising, the task it had claimed was left marked in-progress
        with nobody running it (see ``Poller._recover_orphans``).

        Batch size is the memory knob: ``QAR_EMBED_BATCH`` (default 128). Per-text length is the
        other one: a text far longer than the model's context window costs tokenizer memory to
        produce tokens the model will truncate anyway, so each is capped at ``QAR_EMBED_MAX_CHARS``
        (default 8000) for EMBEDDING only -- the payload keeps the full text, so what search
        returns is unchanged.
        """
        try:
            if not items:
                return
            from qdrant_client.models import PointStruct

            digest = _scope_hash(scope)
            batch_size = embed_batch_size()
            max_chars = embed_max_chars()
            coll: Optional[str] = None
            for start in range(0, len(items), batch_size):
                chunk = items[start:start + batch_size]
                # Cap only what is EMBEDDED. The payload below keeps the full text.
                texts = [(item.get("text", "") or "")[:max_chars] for item in chunk]
                vecs = self._embed_safe(texts)
                if not vecs or len(vecs) != len(chunk):
                    return
                if coll is None:
                    self._adopt_embedding_dim(len(vecs[0]))
                    coll = self._collection_name()
                    self._ensure_collection(coll)
                points: List[PointStruct] = []
                for item, vec in zip(chunk, vecs):
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
                # Drop this batch's vectors before the next one is embedded: holding them would
                # reintroduce, one batch at a time, exactly the whole-corpus peak this avoids.
                del vecs, points, texts
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
            stored_stores: Dict[int, Any] = {}
            for sp in stored_points:
                fp_val = (sp.payload or {}).get("_fingerprint")
                stored_fps[sp.id] = fp_val
                stored_stores[sp.id] = (sp.payload or {}).get(_STORE_KEY)

            # Determine which items need re-embedding.
            to_upsert: List[Dict[str, Any]] = []
            retag: Dict[str, List[int]] = {}
            for item in items:
                nid = _point_id(item["id"], digest)
                new_fp = item.get("fingerprint")
                old_fp = stored_fps.get(nid)
                if nid not in stored_fps or new_fp != old_fp:
                    to_upsert.append(item)
                else:
                    # Same content, so NO re-embedding. But a point embedded before its store had
                    # an identity carries no store id; stamp it (a payload write, not an embedding)
                    # so mounting readers can find it by store.
                    want = (item.get("payload") or {}).get(_STORE_KEY)
                    if want and stored_stores.get(nid) != want:
                        retag.setdefault(str(want), []).append(nid)
            for store_value, point_ids in retag.items():
                try:
                    self._client.set_payload(collection_name=coll, payload={_STORE_KEY: store_value},
                                             points=point_ids)
                except Exception:  # noqa: BLE001
                    logger.debug("QdrantVectorStore: store tagging failed", exc_info=True)

            if not to_upsert:
                return 0

            self.upsert(to_upsert, scope=scope)
            return len(to_upsert)
        except Exception:
            logger.debug("QdrantVectorStore.sync failed", exc_info=True)
            return 0

    def embed_query(self, text: str) -> Optional[List[float]]:
        """The QUERY embedding of ``text`` with this store's query embedder, or None. Never raises."""
        vecs = self._query_embed_safe([text])
        return list(vecs[0]) if vecs else None

    @property
    def vector_dim(self) -> int:
        return int(self._vector_size)

    def existing_sizes(self) -> List[int]:
        """Vector sizes of this prefix's collections that exist on the backend (reads only)."""
        sizes: List[int] = []
        try:
            for coll in self._client.get_collections().collections:
                name = str(coll.name)
                if name == f"{self._prefix}_default" or name.startswith(f"{self._prefix}_default_"):
                    size = self._collection_vector_size(name)
                    if size and size not in sizes:
                        sizes.append(size)
        except Exception:  # noqa: BLE001
            logger.debug("QdrantVectorStore.existing_sizes failed", exc_info=True)
        return sizes

    def use_vector_size(self, size: int) -> None:
        """Point this store at the ``size``-dimensional collection (maintenance commands)."""
        self._vector_size = int(size)
        self._resolved_collection = None

    def relabel_points(self, moves: Sequence[PointMove], *, scope: Optional[Dict[str, Any]] = None,
                       delete_old: bool = False, apply: bool = True) -> Dict[str, int]:
        """Re-key and re-label existing points WITHOUT embedding anything (store merges).

        Each move takes the point stored for ``old_item_id``, re-uses its STORED vector, and writes
        it under ``new_item_id`` with ``store_id`` and ``paths`` replaced. When the point id does
        not change (legacy scheme, same card id) this overwrites the point in place; otherwise it
        is a copy, and the old point is deleted only when ``delete_old``. The embedder is never
        called. Idempotent: a point already at its target (right store, right paths) is counted as
        ``done``. With ``apply=False`` nothing is written and the counts say what would happen.

        Returns counts: ``relabelled`` (same point id, rewritten), ``copied`` (new id), ``done``
        (already at target), ``missing`` (no source point), ``deleted`` (old points removed).
        """
        out = {"relabelled": 0, "copied": 0, "done": 0, "missing": 0, "deleted": 0}
        try:
            if not moves:
                return out
            from qdrant_client.models import PointIdsList, PointStruct
            digest = _scope_hash(scope)
            coll = self._collection_name()
            if not self._client.collection_exists(coll):
                out["missing"] = len(moves)
                return out
            batch = 64
            for start in range(0, len(moves), batch):
                chunk = list(moves[start:start + batch])
                old_ids = {_point_id(m.old_item_id, digest): m for m in chunk}
                new_ids = {_point_id(m.new_item_id, digest) for m in chunk}
                old_found = {p.id: p for p in self._client.retrieve(
                    collection_name=coll, ids=list(old_ids), with_payload=True, with_vectors=True)}
                new_found = {p.id: p for p in self._client.retrieve(
                    collection_name=coll, ids=list(new_ids), with_payload=True, with_vectors=False)}
                points: List[Any] = []
                to_delete: List[int] = []
                for m in chunk:
                    old_pid = _point_id(m.old_item_id, digest)
                    new_pid = _point_id(m.new_item_id, digest)
                    target = new_found.get(new_pid)
                    at_target = (target is not None
                                 and (target.payload or {}).get(_STORE_KEY) == m.store_id
                                 and list((target.payload or {}).get("paths") or []) == list(m.paths))
                    src = old_found.get(old_pid)
                    if at_target:
                        out["done"] += 1
                        if old_pid != new_pid and src is not None and delete_old:
                            to_delete.append(old_pid)
                        continue
                    if src is None or src.vector is None:
                        out["missing"] += 1
                        continue
                    out["relabelled" if old_pid == new_pid else "copied"] += 1
                    if old_pid != new_pid and delete_old:
                        to_delete.append(old_pid)
                    payload = dict(src.payload or {})
                    payload[_STORE_KEY] = m.store_id
                    payload["paths"] = list(m.paths)
                    payload["_id"] = m.new_item_id
                    vec = src.vector
                    if isinstance(vec, dict):  # named vectors are not used by this store
                        vec = next(iter(vec.values()))
                    points.append(PointStruct(id=new_pid, vector=list(vec), payload=payload))
                if apply:
                    if points:
                        self._client.upsert(collection_name=coll, points=points)
                    if to_delete:
                        self._client.delete(collection_name=coll,
                                            points_selector=PointIdsList(points=to_delete))
                out["deleted"] += len(to_delete) if apply else 0
        except Exception:
            logger.debug("QdrantVectorStore.relabel_points failed", exc_info=True)
        return out

    def read_vectors(self, item_ids: Sequence[str], *,
                     scope: Optional[Dict[str, Any]] = None) -> Dict[str, List[float]]:
        """The STORED vectors of ``item_ids`` as ``{item_id: vector}`` (reads only, embeds nothing).

        Items without a stored point are simply absent. Used by ``context upload`` to hand existing
        vectors to another backend. Never raises (returns what could be read).
        """
        out: Dict[str, List[float]] = {}
        try:
            ids = list(item_ids)
            if not ids:
                return out
            digest = _scope_hash(scope)
            coll = self._collection_name()
            if not self._client.collection_exists(coll):
                return out
            for start in range(0, len(ids), 64):
                chunk = ids[start:start + 64]
                by_point = {_point_id(i, digest): i for i in chunk}
                for point in self._client.retrieve(collection_name=coll, ids=list(by_point),
                                                   with_payload=False, with_vectors=True):
                    vec = point.vector
                    if isinstance(vec, dict):
                        vec = next(iter(vec.values()), None)
                    if vec:
                        out[by_point[point.id]] = [float(x) for x in vec]
        except Exception:
            logger.debug("QdrantVectorStore.read_vectors failed", exc_info=True)
        return out

    def existing_item_ids(self, item_ids: Sequence[str], *,
                          scope: Optional[Dict[str, Any]] = None) -> List[str]:
        """Which of ``item_ids`` currently have a point in ``scope``. Reads only; never creates."""
        try:
            digest = _scope_hash(scope)
            by_point = {_point_id(i, digest): str(i) for i in item_ids}
            if not by_point:
                return []
            found = self._client.retrieve(collection_name=self._collection_name(),
                                          ids=list(by_point), with_payload=False)
            return [by_point[p.id] for p in found if p.id in by_point]
        except Exception:
            logger.debug("QdrantVectorStore.existing_item_ids failed", exc_info=True)
            return []

    def delete_items(self, item_ids: Sequence[str], *,
                     scope: Optional[Dict[str, Any]] = None) -> int:
        """Delete the points for ``item_ids`` in ``scope``; returns how many existed. Never raises."""
        try:
            present = self.existing_item_ids(item_ids, scope=scope)
            if not present:
                return 0
            from qdrant_client.models import PointIdsList
            digest = _scope_hash(scope)
            self._client.delete(collection_name=self._collection_name(),
                                points_selector=PointIdsList(points=[_point_id(i, digest)
                                                                     for i in present]))
            return len(present)
        except Exception:
            logger.debug("QdrantVectorStore.delete_items failed", exc_info=True)
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


# The embedders live in their own module; these names stay importable from here.
from .qdrant_embedders import (  # noqa: E402,F401
    EMBED_BATCH_DEFAULT,
    EMBED_MAX_CHARS_DEFAULT,
    FASTEMBED_EMBEDDER_ID,
    embed_batch_size,
    embed_max_chars,
    embedder_id_from_env,
    make_openai_embedder,
    make_voyage_embedder,
    openai_embedder_id,
    positive_int_env,
    voyage_embedder_id,
)
