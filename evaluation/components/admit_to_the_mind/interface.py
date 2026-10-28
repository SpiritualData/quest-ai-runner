"""The admission function (the metric of ``admit-to-the-mind``). Every solution is ONE function:

    admit(case: dict) -> {"view": str}

A case is one step of a request: a ``goal`` (what the request needs), and the ``items`` gathered so far in order, each a
read observation (``rel_path``, ``text``) labelled by the case author as ``needed`` (the goal depends on it),
``near_miss`` (on the same subject, but the goal does not depend on it) or ``out_of_scope`` (it belongs to another
person or another quest and must never be admitted). The function returns the working view the next planner step is
given of those items.

Scoring (``score.py``): an item is ADMITTED when its marker (a short unique phrase in its text) is in the view. A needed
item must be admitted; a near miss and an out-of-scope item should not be. A case passes when every needed item is
admitted, no out-of-scope item is, and no more near misses than the case's ``near_miss_allowed`` are. The labels are the
case author's judgment, and the metric judges one step at a time: it cannot see whether the final answer improved.

An implementation module in ``impls/`` exposes ``build(meter) -> admit``. No solution here calls a model.
"""
