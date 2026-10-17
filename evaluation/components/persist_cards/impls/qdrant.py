"""The live ``QdrantCardRepository`` on an embedded (local folder) Qdrant, behind the ``CardRepository`` port, run
through the shared conformance scenarios (see ``backends.py``). The embedder is a deterministic hash vector, so
no model is involved. Zero model calls."""
from persist_cards.backends import DIMENSION, FolderHarness, hash_embedder, on_backend
from quest_ai_runner.adapters.qdrant_card_repository import QdrantCardRepository


def build(meter):  # noqa: ARG001
    return on_backend(
        FolderHarness,
        lambda harness: QdrantCardRepository(collection="persist_cards", embedder=hash_embedder,
                                             vector_size=DIMENSION, path=harness.root),
        close_on_restart=True)
