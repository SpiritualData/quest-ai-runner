"""Qdrant vector store, embedders: the Voyage AI and OpenAI embedder factories, the embedder id strings
that name a model family (stored on card points), and the memory-bound batch and length knobs
(``QAR_EMBED_BATCH``, ``QAR_EMBED_MAX_CHARS``) every embedding call honours.

``adapters.qdrant_vector_store`` re-exports every name here.
"""
from __future__ import annotations

import logging
import os

from typing import Callable, List, Optional

logger = logging.getLogger("quest_ai_runner.adapters.qdrant_vector_store")


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


# How many texts may be in the embedder at once. This is a MEMORY bound, not a throughput one.
#
# A transformer embedder's attention tensor is ``batch x heads x seq^2 x 4`` bytes, so at the
# default model's 512-token window each text in flight costs on the order of 12MB -- and the ONNX
# arena that serves those allocations grows to the high-water mark and does not give it back.
# MEASURED here over 5,678 card-sized texts (peak RSS of the whole process):
#     whole corpus in one call   5571MB in production, and an allocation failure under a 3GB cap
#     batch 128                  a single 1.5GiB attention allocation -> failure
#     batch 32                   a single 384MB attention allocation -> failure
#     batch 16                   completed cleanly, 797MB
# 16 is therefore the default: the first size whose largest single allocation fits comfortably,
# chosen from measurement rather than from what looks like a reasonable round number. Raising it
# trades memory headroom for fewer round trips; a deployment with more RAM can say so with
# ``QAR_EMBED_BATCH``.
EMBED_BATCH_DEFAULT = 16
# Per-text cap for EMBEDDING only (payloads keep the full text). The default model truncates at
# its context window regardless, so anything beyond this costs tokenizer memory for tokens that
# are then discarded.
EMBED_MAX_CHARS_DEFAULT = 8000


FASTEMBED_EMBEDDER_ID = "fastembed:BAAI/bge-small-en-v1.5"


def voyage_embedder_id(model: Optional[str] = None) -> str:
    return f"voyage:{model or os.getenv('VOYAGE_MODEL', 'voyage-3-lite')}"


def openai_embedder_id(model: Optional[str] = None) -> str:
    return f"openai:{model or os.getenv('QAR_OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')}"


def embedder_id_from_env() -> str:
    """The embedder family the environment selects (``QAR_EMBEDDER_BACKEND``), as an id string."""
    backend = (os.getenv("QAR_EMBEDDER_BACKEND") or "").strip().lower()
    if backend == "voyage":
        return voyage_embedder_id()
    if backend == "openai":
        return openai_embedder_id()
    return FASTEMBED_EMBEDDER_ID


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
