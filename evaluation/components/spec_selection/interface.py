"""The spec-selection function. Every implementation is ONE function with the signature of ``core.spec_selection.select_specs``:

    select(task: str, index: dict, *, provider, model, vector_search, ...) -> SpecSelection

The index is ``fixture_index.json``: the real output of ``esoi spec index`` over the evolvable-structure repository's own
records, so a changed index format fails here. The cases are labelled requests: the records each request concerns, a ceiling
on how many ids may come back, and requests that concern nothing. Kinds: direct (the record's own words), paraphrase (other
words, so lexical alone struggles), solution (asks for what a solution does), none (nothing may be returned).

The metric is REAL: the judge is the runner's own model client making real cheap-model calls and the vector search is a real
local embedding search. A case passes when every expected id is in the answer and no more than the ceiling comes back (a model
that returns everything fails). Scripted-judge plumbing tests (batching, invented ids, failed calls) live in
``tests/test_spec_selection.py``, not here. Each returns ``{"ids", "n_ids", "calls", ...}`` for ``esoi.expect``.
"""
