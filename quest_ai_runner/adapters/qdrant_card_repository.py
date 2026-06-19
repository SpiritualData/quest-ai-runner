"""QdrantCardRepository -- a ``CardRepository`` backed by a Qdrant collection.

Install the optional extra to use these classes::

    pip install 'quest-ai-runner[qdrant]'

This is a GENERIC, reusable persistence backend for context cards (see ``card_repository`` and
``file_context_store``). It implements the runner's ``CardRepository`` Protocol (``load_all`` /
``read`` / ``write`` / ``delete`` / ``exists`` / ``revision`` + the OPTIONAL native
``search_cards``) so a ``FileContextStore`` routes ALL card persistence through Qdrant -- with NO
``cards_dir`` filesystem dependency. Cards live as points in ONE Qdrant collection, with an OPTIONAL
``scope`` payload filter for multi-tenant isolation (e.g. ``{"user_id": "..."}``) so a scoped card
can never leak across tenants.

CONNECTION (consumer supplies it, exactly like ``QdrantVectorStore``)
---------------------------------------------------------------------
Pass EITHER an existing ``client=`` (a ``qdrant_client.QdrantClient``, e.g. a shared production
client), OR ``url=`` / ``api_key=`` for a remote/self-hosted server, OR neither for an EMBEDDED
local-filesystem Qdrant under ``path`` (default ``<cwd>/.quest-context/cards-qdrant``). The repo
NEVER reads connection details from the environment itself -- the consumer wires them (per the repo
convention: generic implementation here, consumer supplies only config/connection).

EMBEDDING (consumer supplies the embedder)
------------------------------------------
``embedder`` is a callable ``texts -> vectors`` (e.g. ``make_voyage_embedder(input_type="document")``
or ``make_openai_embedder()``). The repo OWNS embedding on ``write``: it derives the card's
embed-text via the SHARED ``card_repository.card_embed_text`` helper (so it MATCHES
``FileContextStore.export_for_embedding``), embeds it ONCE, and stores the vector on the point. The
query-only ``QdrantCardVectorStore`` then searches the SAME collection without re-embedding the
cards, so each card carries exactly one embedding produced at write time.

POINT SHAPE (one Qdrant point per card)
---------------------------------------
  * ``id``      -- a deterministic unsigned-int from ``(scope, card_id)`` (sha256 -> int), so the
                  same card always maps to the same point (idempotent upsert) and two tenants'
                  identically-named cards are DIFFERENT points (defense in depth on the scope filter).
  * ``vector``  -- the document embedding of the card's shared embed-text.
  * ``payload`` -- the full card dict, PLUS the ``scope`` key/values, ``card_id``, a flat
                  ``_search_text`` field (full-text indexed -> native ``MatchText`` keyword search),
                  and ``updated_at`` (epoch seconds, used by ``revision()`` as a change-stamp).

GRACEFUL DEGRADATION
--------------------
Every method is BEST-EFFORT and NEVER raises (mirroring the filesystem repo's contract): a reader
returns ``None`` / ``{}`` / ``False`` and a writer returns ``False`` on any failure (Qdrant
unavailable, embedder down, etc.). ``search_cards`` returns ``None`` on any failure so the store
falls back to in-app IDF over ``load_all()``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import VectorHit, VectorStoreBase
from .card_repository import card_embed_text

logger = logging.getLogger(__name__)

# The flat searchable text field that carries the full-text index (used by ``search_cards``).
_SEARCH_TEXT_FIELD = "_search_text"

# Scroll page size for ``load_all`` (a single tenant rarely has many cards; one page usually suffices).
_SCROLL_PAGE = 256

# Hard cap on points scrolled per tenant in ``load_all`` (defense against an unbounded scan).
_MAX_SCOPE_CARDS = 2000

# Repo-internal payload fields stripped before a stored card dict is returned to the store.
_INTERNAL_FIELDS = {"card_id", _SEARCH_TEXT_FIELD, "updated_at"}


def _point_id(scope: Dict[str, Any], card_id: str) -> int:
    """Deterministic unsigned-int point id from ``(scope, card_id)``.

    Hashing BOTH the scope and the card id means a card is unique PER SCOPE even if two tenants pick
    the same logical card id, and re-writing the same card overwrites its own point (idempotent
    upsert). The scope is serialized stably (sorted) so the id is order-independent.
    """
    scope_key = "|".join(f"{k}={v}" for k, v in sorted((scope or {}).items()))
    digest = hashlib.sha256(f"{scope_key}\x00{card_id}".encode("utf-8", errors="replace")).hexdigest()
    # 60 hex chars would overflow; take the leading 15 (matches QdrantVectorStore's scheme).
    return int(digest[:15], 16)


def _connect(
    *,
    client: Any = None,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    path: Optional[str] = None,
) -> Any:
    """Build (or reuse) a Qdrant client, mirroring ``QdrantVectorStore``'s connection options.

    Priority: an explicit ``client`` is reused as-is; else a remote ``url`` (with optional
    ``api_key``); else an EMBEDDED local Qdrant under ``path``. Lazy-imports ``qdrant-client`` with a
    clear install hint so importing this module never fails when the extra is absent.
    """
    if client is not None:
        return client
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover - import-time guard
        raise ImportError(
            "QdrantCardRepository requires qdrant-client. "
            "Install with: pip install 'quest-ai-runner[qdrant]'"
        ) from exc
    if url:
        return QdrantClient(url=url, api_key=api_key)
    return QdrantClient(path=path or ".quest-context/cards-qdrant")


class QdrantCardRepository:
    """A generic ``CardRepository`` over a Qdrant collection, optionally scoped to a tenant.

    All operations are best-effort and NEVER raise. When ``scope`` is set, reads/writes/deletes are
    HARD-SCOPED to it via a payload filter (and the point id incorporates the scope), so a card can
    never cross tenants. The repo OWNS embedding on ``write`` (one document embedding per card via
    the injected ``embedder``), so the vector arm can search the same collection without re-embedding.

    Parameters
    ----------
    collection:
        The Qdrant collection name cards live in (one collection holds all tenants' cards, isolated
        by the ``scope`` payload filter when set).
    embedder:
        Callable ``(texts: List[str]) -> List[List[float]]`` used to embed a card's text on ``write``
        (e.g. ``make_voyage_embedder(input_type="document")``).
    vector_size:
        Dimensionality of the embedding vectors (must match ``embedder``). Default 1024 (Voyage lite).
    scope:
        Optional ``{payload_key: value}`` dict for multi-tenant isolation (e.g. ``{"user_id": ...}``).
        When ``None``/empty the repo operates over the whole collection (no scoping).
    client / url / api_key / path:
        Connection options, mirroring ``QdrantVectorStore``: pass an existing ``client``, OR
        ``url`` (+ optional ``api_key``) for a server, OR neither for an EMBEDDED local Qdrant under
        ``path``.
    """

    def __init__(
        self,
        *,
        collection: str,
        embedder: Callable[[List[str]], List[List[float]]],
        vector_size: int = 1024,
        scope: Optional[Dict[str, Any]] = None,
        client: Any = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        path: Optional[str] = None,
    ) -> None:
        self._collection = (collection or "").strip() or "quest_ai_cards"
        self._embed = embedder
        self._vector_size = int(vector_size)
        self._scope: Dict[str, Any] = dict(scope or {})
        self._client = _connect(client=client, url=url, api_key=api_key, path=path)
        # Bumped on every successful local write so the store's cache invalidates immediately within a
        # process even before the Qdrant-side ``updated_at`` stamp is observed on a re-scroll.
        self._local_rev: int = 0
        self._index_ensured = False

    # ------------------------------------------------------------------
    # Collection + index + filter management (best-effort).
    # ------------------------------------------------------------------

    def _ensure_collection_and_index(self) -> bool:
        """Ensure the cards collection exists AND has a full-text index on the search field.

        Returns True when the collection is usable. The full-text index powers ``search_cards``'s
        native ``MatchText``; a keyword index on each scope key keeps the per-tenant filter fast.
        Both creations are idempotent and best-effort (an already-existing index raises, swallowed).
        """
        try:
            if self._client is None:
                return False
            from qdrant_client.models import (
                Distance,
                PayloadSchemaType,
                TextIndexParams,
                TextIndexType,
                TokenizerType,
                VectorParams,
            )
            existing = {c.name for c in self._client.get_collections().collections}
            if self._collection not in existing:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
                )
            if self._index_ensured:
                return True
            # Full-text index on the flat search field -> enables MatchText keyword search.
            try:
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=_SEARCH_TEXT_FIELD,
                    field_schema=TextIndexParams(
                        type=TextIndexType.TEXT,
                        tokenizer=TokenizerType.WORD,
                        min_token_len=2,
                        max_token_len=20,
                        lowercase=True,
                    ),
                )
            except Exception:  # noqa: BLE001 — already exists / unsupported: fine, fall back later
                pass
            # Keyword index on each scope key -> fast per-tenant filtering.
            for key in self._scope:
                try:
                    self._client.create_payload_index(
                        collection_name=self._collection,
                        field_name=key,
                        field_schema=PayloadSchemaType.KEYWORD,
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._index_ensured = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("QdrantCardRepository: ensure collection/index failed: %s", e)
            return False

    def _scope_filter(self, extra_must: Optional[List[Any]] = None) -> Any:
        """A Qdrant filter that scopes to THIS tenant (plus any extra must-conditions), or None.

        When there is no ``scope`` and no extra condition, returns ``None`` (no filter) so the repo
        works unscoped over the whole collection.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        must: List[Any] = [
            FieldCondition(key=k, match=MatchValue(value=v)) for k, v in self._scope.items()
        ]
        if extra_must:
            must.extend(extra_must)
        return Filter(must=must) if must else None

    @staticmethod
    def _strip_card(payload: Dict[str, Any], scope_keys: Any) -> Dict[str, Any]:
        """Return the stored card dict from a point payload, dropping repo-internal + scope fields."""
        if not isinstance(payload, dict):
            return {}
        drop = _INTERNAL_FIELDS | set(scope_keys)
        return {k: v for k, v in payload.items() if k not in drop}

    def _in_scope(self, payload: Dict[str, Any]) -> bool:
        """Defense in depth: never return a point that isn't this tenant's (id collisions aside)."""
        for k, v in self._scope.items():
            if str(payload.get(k)) != str(v):
                return False
        return True

    # ------------------------------------------------------------------
    # CardRepository implementation (all best-effort, never raise).
    # ------------------------------------------------------------------

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Embed the card text and upsert ONE point {id, vector, payload}. Returns True on success.

        The point id derives from ``(scope, card_id)`` so this is an idempotent upsert. The payload is
        the full card plus the scope key/values, ``card_id``, ``_search_text`` and ``updated_at``.
        Embedding happens HERE (once) via the injected document ``embedder``. Never raises.
        """
        try:
            if not isinstance(card, dict):
                return False
            if not self._ensure_collection_and_index():
                return False

            text = card_embed_text(card)
            # Embed the card text. An empty embed-text (or an embedder failure) means nothing to
            # embed; we still persist the card with a zero vector of the right dim so reads / load_all
            # / search_cards keep working (semantic search simply won't surface a zero-vector card,
            # the correct degraded behavior). The collection requires a vector of the right size.
            vector: Optional[List[float]] = None
            if text:
                try:
                    vecs = self._embed([text])
                    if vecs and vecs[0]:
                        vector = list(vecs[0])
                except Exception as e:  # noqa: BLE001 — embedder down: persist with a zero vector
                    logger.debug("QdrantCardRepository.write: embedding failed: %s", e)
            if not vector:
                vector = [0.0] * self._vector_size

            from qdrant_client.models import PointStruct

            stored = dict(card)
            stored["id"] = card.get("id") or card_id
            payload = {
                **stored,
                **self._scope,
                "card_id": card_id,
                _SEARCH_TEXT_FIELD: text,
                "updated_at": time.time(),
            }
            self._client.upsert(
                collection_name=self._collection,
                points=[PointStruct(id=_point_id(self._scope, card_id), vector=vector, payload=payload)],
            )
            self._local_rev += 1
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("QdrantCardRepository.write failed for %r: %s", card_id, e)
            return False

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve THIS tenant's card by id (scoped). Returns the card dict or None. Never raises."""
        try:
            if not self._ensure_collection_and_index():
                return None
            points = self._client.retrieve(
                collection_name=self._collection,
                ids=[_point_id(self._scope, card_id)],
                with_payload=True,
            )
            if not points:
                return None
            payload = points[0].payload or {}
            if not self._in_scope(payload):
                return None
            card = self._strip_card(payload, self._scope.keys())
            return card if isinstance(card, dict) and card else None
        except Exception as e:  # noqa: BLE001
            logger.debug("QdrantCardRepository.read failed for %r: %s", card_id, e)
            return None

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Scroll THIS tenant's card points -> ``{card_id: card_dict}``. Returns {} on error."""
        out: Dict[str, Dict[str, Any]] = {}
        try:
            if not self._ensure_collection_and_index():
                return {}
            offset = None
            scanned = 0
            while True:
                points, offset = self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=self._scope_filter(),
                    limit=_SCROLL_PAGE,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for p in points:
                    payload = p.payload or {}
                    cid = payload.get("card_id") or payload.get("id")
                    if not cid:
                        continue
                    out[str(cid)] = self._strip_card(payload, self._scope.keys())
                scanned += len(points)
                if offset is None or scanned >= _MAX_SCOPE_CARDS:
                    break
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug("QdrantCardRepository.load_all failed: %s", e)
            return {}

    def delete(self, card_id: str) -> bool:
        """Delete THIS tenant's card point. Returns True when it is gone afterwards. Never raises."""
        try:
            if not self._ensure_collection_and_index():
                return False
            from qdrant_client.models import PointIdsList
            self._client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=[_point_id(self._scope, card_id)]),
            )
            self._local_rev += 1
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("QdrantCardRepository.delete failed for %r: %s", card_id, e)
            return False

    def exists(self, card_id: str) -> bool:
        """True when THIS tenant has a card under ``card_id``. Never raises."""
        return self.read(card_id) is not None

    def revision(self) -> Any:
        """A cheap scoped change-stamp: ``(local_write_counter, scope_card_count, max_updated_at)``.

        Bumps whenever this process writes (``_local_rev``), and also reflects external writes via the
        tenant's point count + the max ``updated_at`` seen on a cheap scroll. The store only needs
        this to CHANGE on any write so its in-memory cache reloads. Returns a stable sentinel on error.
        """
        try:
            if self._client is None:
                return (self._local_rev, 0, 0.0)
            count_res = self._client.count(
                collection_name=self._collection,
                count_filter=self._scope_filter(),
                exact=False,
            )
            count = int(getattr(count_res, "count", 0) or 0)
            max_ts = 0.0
            try:
                points, _ = self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=self._scope_filter(),
                    limit=_SCROLL_PAGE,
                    with_payload=["updated_at"],
                    with_vectors=False,
                )
                for p in points:
                    ts = (p.payload or {}).get("updated_at") or 0.0
                    try:
                        ts = float(ts)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if ts > max_ts:
                        max_ts = ts
            except Exception:  # noqa: BLE001
                pass
            return (self._local_rev, count, max_ts)
        except Exception:  # noqa: BLE001
            return (self._local_rev, 0, 0.0)

    # ------------------------------------------------------------------
    # OPTIONAL native text search (full-text index + MatchText).
    # ------------------------------------------------------------------

    def search_cards(self, query: str, *, limit: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """Native full-text search over THIS tenant's cards -> ``{card_id: card_dict}``, or None.

        Uses a Qdrant FULL-TEXT payload index on ``_search_text`` with ``MatchText`` (AND-scoped to
        the tenant). Returns ``None`` on any failure so ``FileContextStore`` falls back to in-app IDF
        over ``load_all()``. A query that matches nothing returns an EMPTY dict (a valid native
        result), not None. Never raises.
        """
        try:
            q = (query or "").strip()
            if not q:
                return None
            if not self._ensure_collection_and_index():
                return None
            try:
                lim = max(1, int(limit))
            except (TypeError, ValueError):
                lim = 32
            from qdrant_client.models import FieldCondition, MatchText
            flt = self._scope_filter(extra_must=[
                FieldCondition(key=_SEARCH_TEXT_FIELD, match=MatchText(text=q)),
            ])
            points, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=flt,
                limit=lim,
                with_payload=True,
                with_vectors=False,
            )
            out: Dict[str, Dict[str, Any]] = {}
            for p in points:
                payload = p.payload or {}
                cid = payload.get("card_id") or payload.get("id")
                if not cid:
                    continue
                out[str(cid)] = self._strip_card(payload, self._scope.keys())
            return out
        except Exception as e:  # noqa: BLE001 — native search is best-effort; fall back to in-app IDF
            logger.debug("QdrantCardRepository.search_cards failed; falling back to IDF: %s", e)
            return None


# ----------------------------------------------------------------------
# Vector arm: a thin VectorStore that QUERIES the SAME cards collection.
#
# ``QdrantCardRepository`` already embedded + stored each card's vector on write. The card store's
# VECTOR arm must therefore only SEARCH this collection (never re-embed via export_for_embedding /
# sync). This adapter satisfies the runner's ``VectorStoreBase.search`` (the only method
# ``VectorContextAssembler`` needs at query time) by running a scoped Qdrant vector search over the
# cards collection and shaping the hits like the bootstrap card embeddings the assembler expects.
# ``upsert`` / ``sync`` are no-ops (embedding is owned by the repo), so there is exactly ONE embedding
# per card and no double-embedding.
# ----------------------------------------------------------------------

class QdrantCardVectorStore(VectorStoreBase):
    """A query-only ``VectorStore`` over the shared cards collection, optionally scoped to a tenant.

    Implements just enough of the runner's ``VectorStoreBase`` for ``VectorContextAssembler`` to run
    its semantic retrieval at query time. It does NOT embed or upsert cards (the
    ``QdrantCardRepository`` already did that on write), so cards are embedded exactly once.

    Connection + ``query_embedder`` (the search-side embedder, e.g.
    ``make_voyage_embedder(input_type="query")``) are supplied by the consumer, mirroring
    ``QdrantCardRepository``. ``upsert`` / ``sync`` are deliberate no-ops.
    """

    def __init__(
        self,
        *,
        collection: str,
        query_embedder: Callable[[List[str]], List[List[float]]],
        scope: Optional[Dict[str, Any]] = None,
        client: Any = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        path: Optional[str] = None,
    ) -> None:
        self._collection = (collection or "").strip() or "quest_ai_cards"
        self._query_embed = query_embedder
        self._scope: Dict[str, Any] = dict(scope or {})
        self._client = _connect(client=client, url=url, api_key=api_key, path=path)

    def _scope_filter(self) -> Any:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        must = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in self._scope.items()]
        return Filter(must=must) if must else None

    def search(self, query: str, *, scope: Optional[Dict[str, Any]] = None, top_k: int = 8) -> List[VectorHit]:
        """Scoped Qdrant vector search over the cards collection. Returns ``VectorHit``s. Never raises."""
        try:
            q = (query or "").strip()
            if not q or self._client is None:
                return []
            try:
                vecs = self._query_embed([q])
            except Exception as e:  # noqa: BLE001
                logger.debug("QdrantCardVectorStore: query embedding failed: %s", e)
                return []
            if not vecs or not vecs[0]:
                return []
            results = self._client.query_points(
                collection_name=self._collection,
                query=list(vecs[0]),
                query_filter=self._scope_filter(),
                limit=max(1, int(top_k or 8)),
                with_payload=True,
            ).points
            hits: List[VectorHit] = []
            for r in results:
                payload = dict(r.payload) if r.payload else {}
                card_id = payload.get("card_id") or payload.get("id") or str(r.id)
                # Shape the payload like a bootstrap card embedding so the assembler renders it well.
                paths = [
                    fe.get("path", "")
                    for fe in (payload.get("files") or [])
                    if isinstance(fe, dict) and fe.get("path")
                ]
                text = payload.get(_SEARCH_TEXT_FIELD) or payload.get("summary") or ""
                hit_payload = {
                    "paths": paths,
                    "summary": payload.get("summary", "") or payload.get("name", ""),
                    "kind": "bootstrap",
                }
                hits.append(VectorHit(id=f"card:{card_id}", score=float(r.score), text=text, payload=hit_payload))
            return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("QdrantCardVectorStore.search failed: %s", e)
            return []

    # Embedding is owned by the repo (one embedding per card). These are deliberate no-ops so the
    # VectorContextAssembler's cold-start seed/record path never re-embeds the cards.
    def upsert(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> None:
        return None

    def sync(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> int:
        return 0

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        try:
            if self._client is None:
                return 0
            res = self._client.count(
                collection_name=self._collection,
                count_filter=self._scope_filter(),
                exact=False,
            )
            return int(getattr(res, "count", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0
