"""The metric-draft function (the metric of ``draft-a-metric``). Every solution is ONE function:

    draft(case: dict) -> dict

A case is one task asking for a draft value of one metric (``cases.json``): the task text and the metric card the runner reads for it
(questions and scales, recent values, the notes, goals and answered asks since the last update). The returned record is what the real
``metric_draft.MetricDraft.run`` produced:

  status      "proposed" (a value was raised for the person to confirm), "nothing" (thin evidence, no proposal) or "failed"
  values      the proposed value for each question, or None
  evidence    the cited evidence of the proposal
  model_calls the number of model calls the turn made

The real ``MetricDraft`` runs in every arm (the card read, the parse that refuses a proposal with no values or no specific evidence,
the proposal call): an arm decides only what stands at the model boundary, the provider whose ``plan`` makes the one structured call.
The metric's own signal is the owner accepting a draft, a host record that does not exist offline: the label of a case is what a
source (a test) says the right draft is, or that the evidence is too thin for one. An implementation module in ``impls/`` exposes
``build(meter) -> draft``.
"""
