"""Local, offline embedder shared by the vector-based context-selection solutions.

``BAAI/bge-small-en-v1.5`` through fastembed (ONNX), read from the fastembed cache with ``HF_HUB_OFFLINE``
set: if the model is missing, ``embedder()`` raises instead of reaching for the network, so a missing model
is a visible failure, not a 0% score. Vectors are cached by text hash in ``/tmp/qar_components`` and saved
every batch, so a rerun or an interrupted run resumes instead of re-embedding ~600 cards (about a minute).
"""
import hashlib
import json
import os
from pathlib import Path

MODEL = "BAAI/bge-small-en-v1.5"
CACHE_FILE = Path("/tmp/qar_components/embed_cache_bge_small.json")


def embedder():
    """Return ``embed(texts) -> numpy array (len(texts), dim)``, cached by text hash."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    from fastembed import TextEmbedding
    try:
        model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"local embedder {MODEL} is not available offline: {e}") from e
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.is_file() else {}

    def embed(texts):
        keys = [hashlib.sha256(t[:2000].encode()).hexdigest() for t in texts]
        todo = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in cache]
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            for (k, _), v in zip(batch, model.embed([t[:2000] for _, t in batch])):
                cache[k] = [float(x) for x in v]
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(cache))
        return np.array([cache[k] for k in keys])
    return embed
