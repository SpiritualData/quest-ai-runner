"""The context-selection function. Every solution is ONE function with this signature:

    select(task: str, corpus: Corpus) -> list[str]

``corpus`` (see ``corpus.py``) is the indexed source tree: ``corpus.root`` (a directory), ``corpus.files``
(repo-relative paths) and ``corpus.cards`` (one deterministic card per file: path, symbols, docstring
summary and description, a test-file weight). A solution may use any of it, or ignore the cards and
read the files itself (the blind-grep floor does).

Returns repo-relative file paths, best first. The scorer reads the first 3. An empty list is a valid
answer ("nothing confident enough"), and counts as a miss.

An implementation module in ``impls/`` exposes ``build(meter) -> select``; it counts any model call it
makes with ``meter.record()``. The solutions here make none.
"""
TOP_K = 3
