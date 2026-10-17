"""The feedback-loop function. Every solution is ONE function with this signature:

    replay(steps: list[dict]) -> {"turns": [str, ...]}

A case is a synthetic sequence of steps in order. Two kinds:

  {"op": "correct", "text": ..., "location": ..., "dispositions": [[disposition, note], ...]}
      A person corrects the assistant and the run's receipt says what each ask in it was. ``standing`` is an
      instruction for all future work ("from now on"); ``done`` is a one-time request that must NOT become a
      rule. A note holding both lists two dispositions, and ``note`` on the standing one is the run's own words
      for the rule half (empty means none, so nothing safe can be carded).
  {"op": "turn", "message": ...}
      A later turn arrives. The function returns, for it, the guidance text the assistant was shown for
      that turn (empty string when none).

``turns`` holds one string per ``turn`` step, in order. The scorer (``score.py``) checks each turn against
its ``want`` substrings (the correction must have come back: the repeat-correction rate is the share of
relevant turns where it did) and its ``avoid`` substrings (corrections that do not bear on this turn must
not be shown, or guidance is noise and the card limit crowds out the useful rule).

This one component measures two solution ids, many-to-one (see README.md): ``store-feedback`` (a
correction is recorded in the feedback store and a standing one becomes a guidance card) and
``guide-by-feedback`` (the guidance provider surfaces it to the next relevant turn). The metric is the
whole loop because neither half is worth anything without the other.

An implementation in ``impls/`` exposes ``build(meter) -> replay``. No solution here calls a model.
"""
