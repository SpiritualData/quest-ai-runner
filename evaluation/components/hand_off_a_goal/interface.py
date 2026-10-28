"""The goal hand-off function (the metric of ``hand-off-a-goal``). Every solution is ONE function:

    hand_off(case: dict) -> dict

A case is one goal on a shared quest (``cases.json``), of one of two kinds, each reaching the shipped code at the model boundary:

  assignee   a goal is created and the code names who it belongs to (``goal_handoff.choose_assignee``). Returns
             {"assignee": member id or None, "ai_can_do": bool}.
  doability  a goal already assigned to a person is judged for their AI representative (``goal_handoff.run_goal_handoff``).
             Returns {"ai_can_do": bool, "decided": bool}; ``ai_can_do`` is true only when the backend would be asked to
             hand the goal to the representative.

The label of a case is what a source (a test, the prompt's own worked example) says the right verdict is. The metric's own
signal, a person reassigning or reversing a verdict, is a host record that does not exist offline, so this component measures
agreement with those labels and says so.

The real ``goal_handoff`` code runs in every arm: an arm decides only what stands at the model boundary, the judge that turns a
prompt into text (a live model, or none). An implementation module in ``impls/`` exposes ``build(meter) -> hand_off``.
"""
