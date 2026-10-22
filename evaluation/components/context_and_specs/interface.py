"""The search-cards-and-specs function. Every implementation is ONE function:

    search(task: str, tags: list) -> {"items": [key, ...], "linked": {key: brought_by}, "no_solution_yet": bool,
                                      "calls": int, "route": str}

``items`` is everything the arm puts in a run's view, in the order a run reads it: each ranked item followed by the
records it brought in by a link. A card's key is ``card:<id>``, a record's key is its id. ``tags`` are the tag or problem
ids a request names (the R3 case); an arm with no use for tags ignores them. ``no_solution_yet`` says the arm told the run
that the best match is a problem or an intent that no solution serves.

The world is fixed (``world.py``): generic fixture cards, the spec_selection fixture index plus a problem and an intent
that no solution serves, a fixture tag table, and the local offline vector search. An implementation module in
``impls/`` exposes ``build(meter) -> search``; the offline arms call no model.
"""
