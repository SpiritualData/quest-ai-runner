"""The deep-run contract function. Every solution is ONE function with this signature:

    contract(kind: str, args: dict) -> dict

The deep run is the expensive, autonomous half of QAR. This component measures the DETERMINISTIC rules
around it (a worker is never started, no model is called): how a worker's outcome becomes "met" or "not
met", when the goal loop stops, how a stuck or limit-hit run is recognised, and what the headless prompt
guarantees. The model-judged half (does this live log say the worker should stop?) is ``deep_review``.

Kinds and the dict a solution returns:
  review_parse      args {text}                        -> {"stop": bool|None, "reason"}   (watchdog reply)
  review_fail_open  args {reply}                       -> {"stop", "reason"}  (``_review_liveness`` with a
                                                          scripted reviewer; "__raise__" makes it raise)
  escalation_id     args {output}                      -> {"id": str|None}
  resume_missing    args {returncode, output, stderr}  -> {"missing": bool}
  envelope_no_work  args {raw}                         -> {"no_work": bool}
  worker_output     args {raw}                         -> {"text", "tokens", "cost", "is_error", "subtype"}
  goal_prompt       args {goal, brief, preamble?, budget_tokens?} -> {"text": the headless prompt}
  goal_contract     args {result | "__raise__"}        -> {"met", "error", "decision_id"}  (GoalRunner.run)
  goal_kwargs       args {runner: "plain"|"full"}      -> {"forwarded": sorted extra kwargs the runner got}
  goal_loop         args {runs, verdicts, max_iterations} -> {"runner_calls", "verify_calls", "final_met"}
                    (a real Orchestrator with a scripted runner and verifier: the done-standard loop)

An implementation module in ``impls/`` exposes ``build(meter) -> contract``. No solution here calls a model.
"""
