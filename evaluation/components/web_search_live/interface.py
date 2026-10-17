"""The live web-search function. Every solution is ONE function with this signature:

    search(case: dict) -> dict      # the result dict of web_search.bench.run_adapter

The solution row's ideal is "right passage per query at lowest search spend". The fixture component
(``web_search``) proves every backend handles the same payloads identically; it cannot say whether a real engine
finds the right passage or what that costs. This component runs ``scripts/web_search_bench.py`` (the operator
tool) over the PRODUCTION adapter built from the process environment (``build_web_research_from_env``, the
backend chain a deployment would use), with the cache read bypassed so every query is a real, paid backend call.

A case is a public factual question and the phrases that mark a right answer. Scoring (``score.py``): no error,
an answer phrase appears in the observation the planner would see, and the urls are unique. The run's spend is
the real backend calls times the list price per call in ``pricing.py``, recorded in the cost column.

It spends money, so it is opt-in: without a configured backend (or with ``QAR_EVAL_LIVE_SEARCH`` unset) ``build``
prints SKIPPED and exits 0, and the offline suite never reaches it. Run a ladder, never the whole set first:
``--only WL-01,WL-02,WL-03``, then ten.
"""
