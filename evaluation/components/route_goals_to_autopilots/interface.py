"""The goal routing function. Every solution is ONE function with this signature:

    route(case_args: dict) -> dict

Goals follow scope: each day, every eligible goal (current period, incomplete, marked for AI help) is given to the ONE on-duty autopilot whose scope it fits,
or to none, by a fast-tier judgment; a goal a rep holds stays with an on-duty autopilot of that rep. This component scores the routing code around the judge
(eligibility, the duty set, one autopilot per goal, a rep's goal, unknown answers, a failed judgment) with a SCRIPTED judge, so it makes no model call; how well
a real judge reads scope is measured by running the same cases with a model provider, by decision.

``case_args`` keys: ``goals`` (period goals), ``duty`` (the autopilots on duty: ``id``, ``name``, ``purpose``, ``instructions``, optional ``rep_id``),
``judge`` (what the scripted judge answers: ``{goal id: autopilot id or null}``, a list when it answers twice, or ``"FAIL"`` when it raises).

It returns ``{"assignments": {goal id: autopilot id}}``.

An implementation module in ``impls/`` exposes ``build(meter) -> route``.
"""
