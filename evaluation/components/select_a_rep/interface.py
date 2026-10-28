"""The representative-selection function (the metric of ``select-a-rep``). Every solution is ONE function:

    select(case: dict) -> {"rep_id": str | None}

A case names a ``registry`` (the representatives a lane knows, in the rich or flat registry shape), a queued ``task``
(a dict, as the poller hands it over), optional domain ``cards`` (card id -> card json, written to a scratch folder) and
the lane policy ``card_activation`` (on or off). The function returns the id of the one representative that should
handle the task, or None when nobody should.

Scoring (``score.py``) compares it with the case's labelled ``expect``. A case marked ``known_gap`` is a behaviour the
solution's text asks for that no implementation has today (a spawned task keeping its thread's representative, a
representative the person cannot see never chosen); it is kept so that a fix shows up as a passing case.

An implementation module in ``impls/`` exposes ``build(meter) -> select``. No solution here calls a model: the
explicit-ask judge is a model step, so it is left off in every case and is not measured here.
"""
