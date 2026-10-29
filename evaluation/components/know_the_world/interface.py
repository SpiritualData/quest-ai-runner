"""The know-the-world function. Every solution is ONE function with this signature:

    answer(case: dict) -> {"text": str, "cited": [url], "retrieved": [url]}

A case is a world question, the search pages the backend would answer with (the fixtures of ``web_search/cases.json``, loaded by
``source`` id and never copied), the claim a right answer rests on (``claim``) and whether the retrieved pages support it
(``supported``). The function answers the way a turn does: it reads what the web research returned and states what that supports,
citing the source inline as ``[title](url)``, or says plainly that it found no source. It returns the answer text, the urls
it cited and the urls the research actually retrieved.

What this measures is the first link of "evidence-weighted knowledge": a claim about the world is stated only with a retrieved
source that contains it, and a claim no retrieved source supports is not stated. It does not measure confidence calibration or
live relevance (the fixtures are written pages), and the answer text is composed by rule from the observation, so how well a real
model writes from the same observation is not read. An implementation module in ``impls/`` exposes ``build(meter) -> answer``.
No solution here calls a model or the network.
"""
