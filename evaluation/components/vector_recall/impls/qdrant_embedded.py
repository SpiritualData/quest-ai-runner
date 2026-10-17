"""The live ``QdrantVectorStore`` (the index-by-meaning implementation) in its embedded, on-disk mode in a temp
directory: ``sync`` the handbook cards, ``search`` the query. The embedder is the local bge-small ONNX model
(offline, cached); Qdrant's own client, collection, scoping and cosine search run for real. Nothing is faked.
Document vectors come from the text-hash cache; once the corpus is indexed, every QUERY is embedded by the model
itself (uncached), so a timed search pays for the real embedding."""
import tempfile

from context_selection.embedder import embedder, uncached_embedder
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

from retrieval.world import markdown
from vector_recall.interface import TOP_K


def build(meter):  # noqa: ARG001
    cached, fresh = embedder(), uncached_embedder()
    stores = {}  # one indexed store per corpus (keyed by its size and first and last ids)

    def search(query, corpus):
        docs = corpus["docs"]
        key = (len(docs), docs[0]["id"], docs[-1]["id"])
        if key not in stores:
            ready = {"indexed": False}

            def to_lists(texts):
                return [list(map(float, v)) for v in (fresh if ready["indexed"] else cached)(list(texts))]

            store = QdrantVectorStore(path=tempfile.mkdtemp(prefix="qar_vector_recall_"), embedder=to_lists,
                                      vector_size=384, embedder_id="bge-small-en-v1.5")
            store.sync([{"id": d["id"], "text": markdown(d), "fingerprint": d["id"]} for d in docs])
            ready["indexed"] = True
            stores[key] = store
        return {"ids": [h.id for h in stores[key].search(query, top_k=TOP_K)]}
    return search
