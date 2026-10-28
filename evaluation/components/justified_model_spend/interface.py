"""The justification function (the metric of ``justify-model-calls``, ``metric-justified-model-spend``). Every solution is ONE function:

    justify(case: dict) -> dict

A case is one registered prompt spec (``spec_id``) with the owner arm that ships it (``keep``), the arm with its call
dropped or downgraded (``ablation``), the case kinds of the owner component that exercise it, and the prompt's own budget
with the file that states it. The function returns:

  verdict       "pass" or "fail" (a prompt is never "unmeasured" here: prompts with no case are listed UNMEASURED by
                ``score.unmeasured``)
  reasons       why it failed, empty on a pass
  avg_spend     average dollars per turn the prompt costs (fire rate times dollars per call), the weight of the prompt
  stats         fire rate per turn, tokens in and out, dollars and seconds per call, each average and maximum

Scoring (``score.py``): a prompt passes when it is registered, its owner metric has a current row, its call beats its
ablation on the owner's cases (more cases right, not equal), and its maximum cost per turn and maximum seconds per call
stay inside its own budget. The score of a run is the share of average spend on prompts that pass (``score.spend_share``).
The stats come from per-call traces of eval cases (``measure.py``), not production turns, and say so.

An implementation module in ``impls/`` exposes ``build(meter) -> justify``. No arm here calls a model: the paid runs are
``measure.py``'s, made once and kept in ``measurements/``.
"""
