"""The terminal-helper function (the metric of ``assist-a-terminal-session``). Every solution is ONE function:

    assist(case: dict) -> dict

The case ``kind`` says which helper the terminal needs:

  ack       the ``send`` command queued a request and prints a sentence. Returns {"stdout", "exit"}.
  classify  one open ask is sorted for ``reconcile-plan``. Returns {"raw": the answer dict or None, "decision": the
            decision the planner would apply after its own checks or None}.
  judge     one assigned goal is judged AI-doable or not for ``reconcile-plan``. Returns {"raw": dict or None}.
  gate      a list of asks goes through the planner's classify step. Returns {"sent": keys handed to the classifier}.

The real command code runs in every arm (``cli_quest._cmd_send``, ``cli_config._ask_classifier`` and ``_goal_judge``,
``plan_reconcile.classify_asks``). An arm decides only what stands at the model boundary: the provider those functions
call (a replay of the label, a live model, or none) or, for the rules floor, the function that replaces the model.

Scoring (``score.py``) compares the result with the case's ``expect`` or ``label``, both taken from a source the case
names. An implementation module in ``impls/`` exposes ``build(meter) -> assist``.
"""
