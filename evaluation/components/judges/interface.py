"""The judges function. Every solution is ONE function with this signature:

    judge(kind: str, args: dict) -> dict

The small judges (overseer, reach judge, quest judge, sufficiency gate) each ask a model one narrow
question; around that question sits deterministic code that decides whether the answer is usable. This
component measures that deterministic half, with a SCRIPTED provider standing in for the model, so no
model call is made and the same labelled cases run in CI. What the model decides (is this draft really
an answer?) is measured by ``overseer_signals`` (live arm, waived: it needs a model).

Kinds and the dict a solution returns:
  overseer_parse   args {raw}                 -> {"signal", "hint", "reason", "degraded"}
                   ``raw`` is what the scripted model returned ("__raise__" makes the call raise).
  overseer_digest  args {digest kwargs}       -> {"text": the digest handed to the model}
  reach_parse      args {raw}                 -> {"verdict": {"reach", "covered_by"} | None}
  reach_block      args {verdict, web}        -> {"text": the block stamped into the planner prompt}
  quest_rank       args {items, quests, home, fallback, raw} -> {"results": [[chosen, ranked], ...]}
  sufficiency      args {cards, executed}     -> {"pending": [labels still to fetch], "notice": bool}

An implementation module in ``impls/`` exposes ``build(meter) -> judge``. No solution here calls a model.
"""
