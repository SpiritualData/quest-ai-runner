"""ALTERNATIVE: semantic search with the local ``BAAI/bge-small-en-v1.5`` embedder (fastembed, ONNX, the same
model and the same card embed text the production vector arm uses), cosine over one vector per file card.
Runs fully offline once the model is in the fastembed cache; if it is not, ``build`` raises rather than
reaching for the network (``HF_HUB_OFFLINE`` is set), so a missing model is a visible failure, not a 0%.

Embedding ~600 cards takes about a minute, so vectors are cached by text hash in ``/tmp/qar_components`` and
saved every batch: a rerun, or a run that was interrupted, resumes instead of starting over."""
import hashlib
import json
import os
from pathlib import Path

MODEL = "BAAI/bge-small-en-v1.5"


def build(meter):  # noqa: ARG001
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    from fastembed import TextEmbedding
    from quest_ai_runner.adapters.card_repository import card_embed_text
    try:
        model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"local embedder {MODEL} is not available offline: {e}") from e
    index = {}
    cache_file = Path("/tmp/qar_components/embed_cache_bge_small.json")
    cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}

    def embed_cached(texts):
        keys = [hashlib.sha256(t[:2000].encode()).hexdigest() for t in texts]
        todo = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in cache]
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            for (k, _), v in zip(batch, model.embed([t[:2000] for _, t in batch])):
                cache[k] = [float(x) for x in v]
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache))
        return np.array([cache[k] for k in keys])

    def select(task, corpus):
        key = str(corpus.root)
        if key not in index:
            ids = list(corpus.cards)
            texts = [card_embed_text(corpus.cards[i]) or corpus.cards[i]["files"][0]["path"] for i in ids]
            index[key] = ([corpus.cards[i]["files"][0]["path"] for i in ids],
                          embed_cached(texts))
        paths, mat = index[key]
        q = embed_cached([task])[0]
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
        return [paths[i] for i in np.argsort(-sims, kind="stable")[:10]]
    return select
