"""The think-critically function. Every solution is ONE function with this signature:

    hold(case: dict) -> dict

A case is one request the system serves (``kind``: a ``deep`` run, a ``chat`` turn, an in-process ``task``), whether a metric
applies to it (``has_metric``), whether the request changed anything (``changed``), and the metric's true reading before and
after (``before``, ``after``). The function applies the specification's metric to the request, as the system does, and returns
what is on record afterwards:

  rows       the metric rows the code kept: [{"before", "after", "source"}]; a reading is taken by a meter, never invented
  missing    True when a change ended with no row and the flow marked it
  proposed   True when a metric was proposed for a request that had none
  response   what answered a regression: "" (nothing), "retry", "revert", "follow_up" or "escalate"

The scorer (``score.py``) reads those facts against the case labels; a model's words are never read. An implementation module in
``impls/`` exposes ``build(meter) -> hold``. No solution here calls a model.
"""
