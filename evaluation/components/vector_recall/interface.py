"""The vector-search function. Every solution is ONE function with this signature:

    search(query: str, corpus: dict) -> {"ids": [doc id, ...]}

``corpus`` is the retrieval component's synthetic handbook (``../retrieval/corpus.json``, invented), one card
per document (title, summary and sections as the card text). A solution indexes the cards by meaning with its
vector store and returns the ids nearest the query, best first. The queries are mostly PARAPHRASES that share
no keyword with the answer (the case a keyword search cannot serve), plus a few exact ones. Scoring
(``score.py``): the labelled card is within the top 3 (``VectorStore.search`` with ``top_k`` 3). No model call:
the embedder is the local ``BAAI/bge-small-en-v1.5`` ONNX model from the fastembed cache, offline.

An implementation module in ``impls/`` exposes ``build(meter) -> search``; these make no model call."""
TOP_K = 3
