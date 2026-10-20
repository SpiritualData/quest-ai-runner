"""The spec-selection function. Every implementation is ONE function with the signature of ``core.spec_selection.select_specs``:

    select(task: str, index: dict, *, provider, model, vector_search, ...) -> SpecSelection

The index is ``fixture_index.json``: the real output of ``esoi spec index`` over the evolvable-structure repository's own
records (168 entries), so a changed index format fails here. The model is a SCRIPTED stand-in that answers with the case's
``relevant`` ids among those it was offered (a perfect judge), because the arm measures the PIPELINE (narrowing, fusion,
batching, parallelism, dropping invented ids, surviving a failed call), not a model's taste; it spends nothing and says so
(``meter`` counts the calls a real model would receive). A vector search is likewise a dict the case supplies.
Kinds: lexical, vocabulary_gap, batches, invented, model_failure, links. Each returns
``{"ids", "calls", ...}`` for ``esoi.expect``. Deterministic and offline.
"""
