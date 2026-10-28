"""The long-view carry function (the metric of ``hold-the-long-view``). Every solution is ONE function:

    carry(case: dict) -> {"prompt": str}

A case is what a host hands a run about earlier work: ``escalations`` (the proposals put to the person earlier, each
with an ``outcome``, a ``summary`` and optionally the ``quest_id`` it was made on) and the new ``message``. The function
runs the request and returns the first planner prompt the planner was shown, which is where earlier work either
reaches the next request or does not.

Scoring (``score.py``): every ``want`` string must be in the prompt (a decline held, a correction applied) and no
``avoid`` string may be (nothing from another person, another quest outside the branch, or a proposal that was accepted
being treated as declined). A case marked ``known_gap`` asks for what the solution's text says it should do and no
implementation does today (a decline made on a sibling quest of the same representative holding on this one).

An implementation module in ``impls/`` exposes ``build(meter) -> carry``. No solution here calls a model: the planner is
a scripted stand-in that records the prompt it is given.
"""
