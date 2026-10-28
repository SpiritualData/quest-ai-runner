"""The explanation function (the metric of ``explain-an-answer``, ``metric-explain-an-answer``). Every solution is ONE function:

    explain(case: dict) -> dict

``case`` is one answered turn (``cases.json``): the person's message, the answer, and the turn's trace as the runner kept it. The
function returns:

  payload   the explanation payload the person's panel is built from (``answer_explanation.build_payload``)
  text      that payload read as the person would read it: the written sections and the recorded facts
  record    the record the explanation is constrained to (``render_record_for_prompt``)

The real ``write_answer_explanation`` and ``build_payload`` run in every arm. An arm decides only whether a model writes the
prose half (today, on the Claude Code subscription) or not (the floor: the trace-only payload, a template over the recorded facts).

Scoring (``score.py``): faithful (no step the trace does not hold), complete (no step that changed the answer left out) and, apart,
readable (the prompt's own plain-language contract). An implementation module in ``impls/`` exposes ``build(meter) -> explain``.
"""
