"""The evolve-by-metric function. Every solution is ONE function with this signature:

    evolve(kind: str, args: dict) -> dict

A better part replaces the current one because its metric says so, and the running system then follows that choice.
Kinds and the dict a solution returns:
  challenger  args {selected, rows: {impl: {passed, cases, cases_sha, model_calls, tokens, sample}}}
              -> {"switch_to": impl | None, "status": "switch" | "keep" | "unmeasured"}
              Which implementation the metric says should replace the selected one, if any. A rival counts only on the
              same cases and sample, beyond the noise margin, or equally good and clearly cheaper.
  drift       args {snapshot: {solution: default}, records: {solution: selected}}
              -> {"disagreeing": [solution ids]}   the shipped selection snapshot against the records
  repo_drift  args {} -> {"disagreeing": [ids], "checked": n}
              the repository's own committed ``quest_ai_runner/esoi_selection.json`` against the records and against
              what ``core.resolve`` picks by default
An implementation module in ``impls/`` exposes ``build(meter) -> evolve``. No solution here calls a model.
"""
