"""The retrieval function. Every solution is ONE function with this signature:

    retrieve(question: str, corpus: dict) -> {"ids": [doc id, ...], "texts": {doc id: text read}}

``corpus`` (``corpus.json``) is a small SYNTHETIC handbook (invented, no real data): documents with an id,
a title, a one-line summary and two sections. The SAME documents are exposed through each source type: files
on disk, DB rows, an MCP server, Notion pages, Drive files, Chat threads, a remote file server. A solution
drives the LIVE ``quest_ai_runner`` adapter for its source type over that exposure (only the network edge is
faked), asks the question through the shared gather policy (``policy.py``: question terms, one adapter
search per term, rarity-weighted ranking) and returns document ids best first. ``texts`` holds what the
adapter's own ``read_section`` returned for the top results, so the scorer can check the right section is
readable, not only locatable.

Scoring (``score.py``): recall@3 of the labelled relevant documents (``ok`` = all of them in the top 3), and
the labelled answer phrase present in the text read for each relevant document found. No model, no network.

An implementation module in ``impls/`` exposes ``build(meter) -> retrieve``. These make no model call.
"""
TOP_K = 3
