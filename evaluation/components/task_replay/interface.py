"""The task-replay function (the metric of ``iterate-by-spec``). Every solution is ONE function:

    replay(case: dict) -> dict

A case is a recorded task shape: the goal text and brief as a test or fixture of this repository wrote them, a scratch
workspace built from that fixture, and (where the source test asserted one) the literal condition the finished work must meet.
The arm runs the task through the real deep-run path (the orchestrator's goal loop, a Claude Code worker in the scratch
folder, the done verifier) with the Quest boundary faked, and returns:

  verdicts      every verdict of the done verifier, in order: {"met": bool|None, "error", "reason"}
  spawns        every worker spawn: tokens, the CLI's list-price cost, whether it resumed a session
  changed, created   files changed and files created in the scratch workspace
  work_ok       the case's own work check on the folder after the run (None when the case states none)
  metric_results, metric_missing   what the metric record of the goal holds

Scoring (``score.py``): a case is accepted when the FIRST verdict of the done verifier is met. The arms differ in what the
code does around the goal (the iterate-by-spec implementation, the metric tool, the attempt cap), never in the verifier.
An implementation module in ``impls/`` exposes ``build(meter) -> replay``.
"""
