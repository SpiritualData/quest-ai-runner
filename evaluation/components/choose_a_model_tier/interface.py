"""The model-choice function. Every solution is ONE function with this signature:

    choose(kind: str, args: dict) -> dict

Which model runs a piece of work is decided in a handful of small pure functions: the starting rung for a deep
run from the planner's difficulty rating, a bare family name resolved to the newest live release, a tier
resolved to a model from the live list and the operator's pins, the cheaper tier to fall to when one is
exhausted, whether a model can take images, and whether a failure means "wait, the provider or the allowance
is out" rather than "the task failed". All are deterministic, so this component runs offline with no model.

Kinds and the dict a solution returns:
  start_model    args {difficulty, ladder, models}  -> {"start": model | None, "ladder": [...]}
  family         args {model, live}                 -> {"model": id}
  tier           args {tier, live, pins}            -> {"model": id}   (``live`` is the provider's model list)
  lower_tier     args {tier}                        -> {"tier": name | None}
  vision         args {model}                       -> {"vision": bool}
  outage         args {message}                     -> {"outage": bool}
  usage_limit    args {text, now}                   -> {"limited": bool, "kind": str, "resets_at": ISO | None,
                                                        "resume_at": ISO}
An implementation module in ``impls/`` exposes ``build(meter) -> choose``. No solution here calls a model.
"""
