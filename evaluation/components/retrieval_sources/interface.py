"""The per-source conformance arm of gather-evidence. The signature is the retrieval component's:

    retrieve(question: str, corpus: dict) -> {"ids": [doc id, ...], "texts": {doc id: text read}}

The nine retrieval adapters are COMPLEMENTARY sources, not alternatives: nobody chooses Notion instead of Drive,
a lane mounts the sources it has. So ranking them against each other answers nothing, and the gather-evidence
leaderboard ranks only complete retrieval stacks (``retrieval/``: the composite and its floor). This arm runs the
SAME questions, corpus, gather policy and scorer (all imported from ``retrieval/``, and ``cases.json`` is a link
to the retrieval cases, so the two cannot drift) through each source adapter ALONE. It answers a different
question: does this source, wired by itself, find and read what its own type can hold? Each source passes or
fails against the same bar, and its known limits are pinned as expected failures in
``test_retrieval_components.py``. The leaderboard lists it as an invariant (``invariant.txt``): shown, never
ranked, never a reason to change the selected stack.
"""
