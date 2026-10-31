"""The evolve-by-metric function. Every solution is ONE function with this signature:

    evolve(kind: str, args: dict) -> dict

A better part replaces the current one because its metric says so, and the running system then follows that choice.
Kinds and the dict a solution returns:
  challenger  args {selected, rows: {impl: {passed, cases, cases_sha, model_calls, tokens, sample}}}
              -> {"switch_to": impl | None, "status": "switch" | "keep" | "unmeasured"}
              Which implementation the metric says should replace the selected one, if any. A rival counts only on the
              same cases and sample, and only with a statistically significant gain on the pass rate: a one-sided exact
              paired test when ``per_case`` ({impl: "PPFP", one letter per case}) is given, Fisher's exact test on the pass
              counts otherwise. Cost at equal quality does not switch. Optional ``policy`` ({alpha, min_cases,
              best_guess_switch}) is a stricter solution-level requirement; it can only tighten the default.
  first_selection  args {rows} -> {"selected": impl | None, "basis": "best guess"}
              Nothing is selected yet: the best point estimate is chosen and labelled a best guess; exactly equal leaders
              select nothing.
  drift       args {snapshot: {solution: default}, records: {solution: selected}}
              -> {"disagreeing": [solution ids]}   the shipped selection snapshot against the records
  repo_drift  args {} -> {"disagreeing": [ids], "checked": n}
              the repository's own committed ``quest_ai_runner/esoi_selection.json`` against the records and against
              what ``core.resolve`` picks by default
An implementation module in ``impls/`` exposes ``build(meter) -> evolve``. No solution here calls a model.
"""
