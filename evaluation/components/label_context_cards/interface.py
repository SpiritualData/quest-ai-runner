"""The card-labelling function (the metric of ``label-context-cards``). Every solution is ONE function:

    label(case: dict) -> dict

Two kinds of case (``cases.json``), one per prompt of the solution:

  retrieve  a card has just been created from its sources. The arm writes its metadata (title, description, keywords); the card is
            stored with that metadata and read back through the real ``FileContextStore.assemble`` for each labelled query. Returns
            {"hits": [bool per query that must resolve its collection], "leaks": [bool per query that must resolve nothing],
            "fallback": the metadata was the generator's no-model fallback, "metadata": the metadata}.
  place     files edited by a deep run are placed into the context cards the run used. Returns {"placed": {file: card id or None}}.

The real code runs in every arm (``CardMetadataGenerator``, ``FileContextStore``, ``categorize_files_with_llm``): an arm decides only
what stands at the model boundary, the provider whose answer is the metadata or the placement (a live model, or none, in which case
metadata and placement come from the card's own title and sources and the files' paths). An implementation module in ``impls/``
exposes ``build(meter) -> label``.
"""
