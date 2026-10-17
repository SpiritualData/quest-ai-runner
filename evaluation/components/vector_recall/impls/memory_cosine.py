"""BASELINE: the same bge-small embeddings scored by plain numpy cosine in memory, with no vector database. What
the store must at least match; a gap would be the vector database losing recall or time. Like the store arm, the
documents use the text-hash cache and every query after indexing is embedded by the model (uncached)."""
import numpy as np

from context_selection.embedder import embedder, uncached_embedder

from retrieval.world import markdown
from vector_recall.interface import TOP_K

ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    cached, fresh = embedder(), uncached_embedder()
    indexes = {}

    def search(query, corpus):
        docs = corpus["docs"]
        key = (len(docs), docs[0]["id"], docs[-1]["id"])
        if key not in indexes:
            mat = cached([markdown(d) for d in docs])
            indexes[key] = (mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9), [d["id"] for d in docs])
        mat, ids = indexes[key]
        q = fresh([query])[0]
        sims = mat @ (q / (np.linalg.norm(q) + 1e-9))
        return {"ids": [ids[i] for i in np.argsort(-sims, kind="stable")[:TOP_K]]}
    return search
