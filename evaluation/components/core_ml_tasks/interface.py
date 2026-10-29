"""The core-ml-tasks function. Every solution is ONE function with this signature:

    measured(case: dict) -> {"measured": bool, "rate": float | None, "why": str}

A case is one Quest classification or recommendation task (``task``: the implementation record id under the solution), and the
evaluation component and arm that is said to measure it (``component``, ``arm``; null when none is). The function answers whether
the task is under one measured choice: a labelled case set exists for the component, and its latest recorded row for that arm
holds a pass rate on those labelled cases at or above the bar ``[balance.metric-qar-overall]`` sets. It reads the evaluation
folders and the results log; it runs no model. Where a component already measures a task (the message router is
``classification``) the number is that component's own recorded row, linked, not measured again. An implementation module in
``impls/`` exposes ``build(meter) -> measured``. No solution here calls a model.
"""
