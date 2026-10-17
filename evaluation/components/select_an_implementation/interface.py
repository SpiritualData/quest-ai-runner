"""The implementation-selection function. Every solution is ONE function with this signature:

    select(kind: str, args: dict) -> dict

Which implementation serves a solution is data (the selection table), not an ``if`` at a call site. The caller
names it, a config file overrides it, or the table default applies; a typo fails loudly with the choices; and
a config override switches the LIVE implementation without editing code. All of it is deterministic, so this
component runs offline, with no model.

Kinds and the dict a solution returns:
  by_name       args {solution, name}                 -> {"cls": class name | None, "error": exception name | None}
  by_override   args {solution, overrides, name?}     -> {"cls": ..., "error": ...}   (RunnerConfig.selections style)
  by_default    args {solution}                       -> {"cls": ..., "default": name, "error": ...}
  typo          args {solution, name}                 -> {"error": exception name | None, "lists_choices": bool}
  integrity     args {}                               -> {"unresolvable": [ids], "bad_defaults": [ids], "solutions": n}
                every table entry imports to a class, and every default is one of its implementations
  live_switch   args {site, ...}                      -> {"cls": class name | None}
                the real wiring at a call site, switched only by data: site "model_provider" (args backend),
                "deep_runner" (args selections), "search_backend" (args backend)
An implementation module in ``impls/`` exposes ``build(meter) -> select``. No solution here calls a model.
"""
