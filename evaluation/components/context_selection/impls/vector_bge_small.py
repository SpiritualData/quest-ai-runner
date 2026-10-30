"""ALTERNATIVE: semantic search with the local ``BAAI/bge-small-en-v1.5`` embedder (see ``embedder.py``), plain
cosine over one vector per file card. It re-implements the ranking rather than wrapping the live
``VectorContextAssembler``, so it is a baseline for the semantic arm, not a measurement of the live class
(``hybrid_union`` wraps that). Uses the same card embed text the production vector arm uses."""
from context_selection.embedder import embedder

ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    import numpy as np
    from hubofhubs.recall.card_repository import card_embed_text
    embed = embedder()
    index = {}

    def select(task, corpus):
        key = str(corpus.root)
        if key not in index:
            ids = list(corpus.cards)
            texts = [card_embed_text(corpus.cards[i]) or corpus.cards[i]["files"][0]["path"] for i in ids]
            index[key] = ([corpus.cards[i]["files"][0]["path"] for i in ids], embed(texts))
        paths, mat = index[key]
        q = embed([task])[0]
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
        return [paths[i] for i in np.argsort(-sims, kind="stable")[:10]]
    return select
