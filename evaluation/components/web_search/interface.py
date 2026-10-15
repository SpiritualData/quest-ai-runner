"""The web-search function. Every solution is ONE function with this signature:

    search(case: dict) -> dict

``case`` carries a query and the PAGES a search backend would have answered with, already in each vendor's own
response shape (``payloads``: serper, brave, tavily, searxng). A solution builds its backend, hands it a fake
HTTP callable that serves those payloads (so no request leaves the process), runs the query through
``WebResearchAdapter`` exactly as production does, measures it with the ported ``scripts/web_search_bench.py``
(latency, result count, characters and estimated tokens added to the planner, cache hit, backend calls) and
returns:

    {"urls": [...], "text": the observation the planner sees, "error": str | None, "backend": name,
     "est_tokens": int, "text_chars": int, "cache_hits": int, "backend_calls": int}

What this measures: whether each backend turns the SAME underlying results into the SAME usable observation
(gold page present near the top, capped at max_results, HTML stripped, duplicates collapsed, a vendor summary
kept where the vendor has one), how it fails (empty result, 429), and how many tokens the observation costs.
What it does not: live relevance or freshness of a real search engine. The payloads are written fixtures in
the vendors' documented shapes, not recordings of live traffic. A live comparison needs the vendor keys
(``scripts/web_search_bench.py --queries ...`` on a host that has them) and is waived in waivers.csv S10.

An implementation module in ``impls/`` exposes ``build(meter) -> search`` (and sets ``search.impl`` to its name
so a case can state a per-implementation expectation). No solution here calls a model or the network.
"""
