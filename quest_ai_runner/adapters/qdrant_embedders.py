"""Qdrant vector store, embedders: QAR's environment bindings of the hub's embedder factories and knobs.

The factories, the embedder id strings that name a model family (stored on card points) and the memory-bound defaults are the
hub's (``hubofhubs.recall.embedders``) and read no environment. This module reads the names QAR owns: ``VOYAGE_MODEL``,
``QAR_OPENAI_EMBEDDING_MODEL``, ``OPENAI_API_KEY``, ``QAR_EMBEDDER_BACKEND`` and the batch and length knobs
(``QAR_EMBED_BATCH``, ``QAR_EMBED_MAX_CHARS``) every embedding call honours.

``adapters.qdrant_vector_store`` re-exports every name here.
"""
from __future__ import annotations

import logging
import os

from typing import Callable, List, Optional

from hubofhubs.recall import embedders as hub_embedders
from hubofhubs.recall.embedders import (  # noqa: F401  (same objects, old import path)
    EMBED_BATCH_DEFAULT,
    EMBED_MAX_CHARS_DEFAULT,
    FASTEMBED_EMBEDDER_ID,
)

logger = logging.getLogger("quest_ai_runner.adapters.qdrant_vector_store")


def voyage_model_from_env() -> str:
    return os.getenv("VOYAGE_MODEL", hub_embedders.DEFAULT_VOYAGE_MODEL)


def openai_model_from_env() -> str:
    return os.getenv("QAR_OPENAI_EMBEDDING_MODEL", hub_embedders.DEFAULT_OPENAI_EMBEDDING_MODEL)


def make_voyage_embedder(
    *,
    model: Optional[str] = None,
    input_type: str = "document",
    api_key: Optional[str] = None,
) -> Callable[[List[str]], List[List[float]]]:
    """A Voyage AI-backed embedding callable; ``model`` falls back to ``VOYAGE_MODEL``, then ``voyage-3-lite``.

    ``api_key`` falls back to the ``voyageai`` library's own lookup (``VOYAGE_API_KEY``). Raises ``ImportError`` at
    factory-call time when ``voyageai`` is not installed.
    """
    return hub_embedders.make_voyage_embedder(model=model or voyage_model_from_env(), input_type=input_type, api_key=api_key)


def make_openai_embedder(
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Callable[[List[str]], List[List[float]]]:
    """An OpenAI-backed embedding callable; ``model`` falls back to ``QAR_OPENAI_EMBEDDING_MODEL``, the key to ``OPENAI_API_KEY``.

    OpenAI embeddings are symmetric, so the same callable serves as both ``embedder`` and ``query_embedder``. Raises
    ``ImportError`` at factory-call time when ``openai`` is not installed.
    """
    return hub_embedders.make_openai_embedder(model=model or openai_model_from_env(),
                                              api_key=api_key or os.getenv("OPENAI_API_KEY"))


def voyage_embedder_id(model: Optional[str] = None) -> str:
    return hub_embedders.voyage_embedder_id(model or voyage_model_from_env())


def openai_embedder_id(model: Optional[str] = None) -> str:
    return hub_embedders.openai_embedder_id(model or openai_model_from_env())


def embedder_id_from_env() -> str:
    """The embedder family the environment selects (``QAR_EMBEDDER_BACKEND``), as an id string."""
    return hub_embedders.embedder_id_for(os.getenv("QAR_EMBEDDER_BACKEND") or "", voyage_model=voyage_model_from_env(),
                                         openai_model=openai_model_from_env())


def positive_int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or ``default``. Never raises."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %s", name, raw, default)
        return default
    return value if value > 0 else default


def embed_batch_size() -> int:
    return positive_int_env("QAR_EMBED_BATCH", EMBED_BATCH_DEFAULT)


def embed_max_chars() -> int:
    return positive_int_env("QAR_EMBED_MAX_CHARS", EMBED_MAX_CHARS_DEFAULT)
