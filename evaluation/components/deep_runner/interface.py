"""The deep-worker function. Every solution is ONE function with this signature:

    work(kind: str, args: dict) -> dict

``run-deep-work`` has more than one implementation of the ``DeepRunner`` port: the reference
``SubprocessGoalRunner`` (one ``claude -p`` process per goal) and the opt-in ``AcpDeepRunner`` (an Agent Client
Protocol session). The brain must not be able to tell them apart, so this component runs the SAME scenarios
through each, with the outside peer faked at its process edge (a scripted ``Popen`` for one, a scripted ACP
connection for the other), and scores how every outcome is reported back: a clean finish is met, a silent
no-op is not, a paused-on-a-human run carries its decision id, a partial or crashed run is not met and says
why, a missing binary is a reported failure, and no scenario raises. No worker is started, no model is called.

Kinds and the dict a solution returns:
  run_worker  args {scenario: {output, stderr?, exit?, stop?, spawn_error?, crash?}, want}
              -> {"met": bool, "has_want": the output contains ``want``, "decision_id": str | None,
                  "error_kind": "none" | "no_output" | "missing_binary" | "failed", "raised": bool}
              ``exit`` is the subprocess exit code and ``stop`` the ACP stop reason for the same story
              (a non-zero exit and ``max_tokens`` are both "stopped before finishing"); ``spawn_error`` is
              "missing" for an absent binary; ``crash`` makes the peer raise mid-run.

An implementation module in ``impls/`` exposes ``build(meter) -> work``. No solution here calls a model.
"""
