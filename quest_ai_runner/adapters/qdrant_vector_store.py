"""QdrantVectorStore: QAR's binding of the hub's Qdrant vector store (``hubofhubs.recall.qdrant_store``).

The store is the hub's: embedded or remote Qdrant, one collection per vector size with the scope as a payload filter, the
fingerprint-based ``sync``, the store fence, the maintenance operations. It reads no environment. This module is what stays
with QAR, so that points already stored keep being found:

* the collection prefix ``qar_ctx`` and the embedded store's default folder (``.quest-context/qdrant``);
* the seeded-card scope ``CARD_SEED_SCOPE`` (key ``qar_point_class``), whose digest is part of every stored point;
* the embed batch size and length cap, read from ``QAR_EMBED_BATCH`` and ``QAR_EMBED_MAX_CHARS`` on every embedding call;
* the declaration that this class is the ``qdrant`` implementation of ``index-by-meaning``.

Install the optional extra to use it: ``pip install 'quest-ai-runner[qdrant]'`` (``qdrant-client`` and ``fastembed``). The
embedder factories (``make_voyage_embedder``, ``make_openai_embedder``) and the embedder ids live in ``qdrant_embedders``
and are re-exported here.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from esoi import implementation
from hubofhubs.recall.qdrant_store import QdrantVectorStore as HubQdrantVectorStore
from hubofhubs.recall.qdrant_store import SCOPE_KEY, STORE_KEY, point_id, scope_hash  # noqa: F401  (re-exported)

from ..core.vector_scopes import CARD_SEED_SCOPE
from .qdrant_embedders import (  # noqa: F401  (re-exported: this was their import path)
    EMBED_BATCH_DEFAULT,
    FASTEMBED_EMBEDDER_ID,
    embed_batch_size,
    embed_max_chars,
    embedder_id_from_env,
    make_openai_embedder,
    make_voyage_embedder,
    openai_embedder_id,
    voyage_embedder_id,
)

COLLECTION_PREFIX = "qar_ctx"
DEFAULT_STORE_PATH = ".quest-context/qdrant"


@implementation("index-by-meaning", "qdrant")
class QdrantVectorStore(HubQdrantVectorStore):
    """The hub's Qdrant store under QAR's prefix, default folder, seed scope and environment knobs."""

    seed_scope = CARD_SEED_SCOPE

    def batch_limit(self) -> int:
        return embed_batch_size()

    def text_limit(self) -> int:
        return embed_max_chars()

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        url: Optional[str] = None,
        collection_prefix: str = COLLECTION_PREFIX,
        embedder: Optional[Callable[[List[str]], List[List[float]]]] = None,
        query_embedder: Optional[Callable[[List[str]], List[List[float]]]] = None,
        vector_size: int = 384,
        embedder_id: Optional[str] = None,
    ) -> None:
        try:
            super().__init__(path=path, url=url, collection_prefix=collection_prefix, embedder=embedder,
                             query_embedder=query_embedder, vector_size=vector_size, embedder_id=embedder_id,
                             default_path=DEFAULT_STORE_PATH, seed_scope=CARD_SEED_SCOPE)
        except ImportError as exc:
            raise ImportError(str(exc).replace("hubofhubs[qdrant]", "quest-ai-runner[qdrant]")) from exc
