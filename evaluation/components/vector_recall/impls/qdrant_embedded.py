"""The live ``QdrantVectorStore`` (the index-by-meaning implementation) in its embedded, on-disk mode in a temp
directory: ``sync`` the handbook cards, ``search`` the query. The embedder is the local bge-small ONNX model
(offline, cached); Qdrant's own client, collection, scoping and cosine search run for real. Nothing is faked."""
import tempfile

from context_selection.embedder import embedder
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

from retrieval.world import markdown
from vector_recall.interface import TOP_K


def build(meter):  # noqa: ARG001
    embed = embedder()
    state = {}

    def to_lists(texts):
        return [list(map(float, v)) for v in embed(list(texts))]

    def search(query, corpus):
        if "store" not in state:
            store = QdrantVectorStore(path=tempfile.mkdtemp(prefix="qar_vector_recall_"), embedder=to_lists,
                                      vector_size=384, embedder_id="bge-small-en-v1.5")
            store.sync([{"id": d["id"], "text": markdown(d), "fingerprint": d["id"]} for d in corpus["docs"]])
            state["store"] = store
        return {"ids": [h.id for h in state["store"].search(query, top_k=TOP_K)]}
    return search
