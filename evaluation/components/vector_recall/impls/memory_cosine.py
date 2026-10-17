"""BASELINE: the same bge-small embeddings scored by plain numpy cosine in memory, with no vector database. What
the store must at least match; a gap would be the vector database losing recall."""
import numpy as np

from context_selection.embedder import embedder

from retrieval.world import markdown
from vector_recall.interface import TOP_K

ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    embed = embedder()
    state = {}

    def search(query, corpus):
        if "mat" not in state:
            mat = embed([markdown(d) for d in corpus["docs"]])
            state["mat"] = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
            state["ids"] = [d["id"] for d in corpus["docs"]]
        q = embed([query])[0]
        sims = state["mat"] @ (q / (np.linalg.norm(q) + 1e-9))
        return {"ids": [state["ids"][i] for i in np.argsort(-sims, kind="stable")[:TOP_K]]}
    return search
