"""The step-continuity function (the metric of ``keep-a-working-stream``). Every solution is ONE function:

    run_request(case: dict, world: FakeQuest) -> dict

The function handles ONE multi-step request end to end against the in-memory quest (``quest_ops.fake_quest``, the
real tool catalog with handlers that mutate a dict) and returns
``{"reply": str, "steps": int, "tokens_in": int, "tokens_out": int}``, where ``steps`` is how many planner calls the
request took. The case carries what the request established before its later steps: ``context_view`` (standing
corrections, scope notes), ``transcript`` (earlier turns, including a refusal) and ``prior_escalations`` (proposals
the person declined).

Scoring (``score.py``) reads the WORLD's tool-call receipts afterwards, never the reply's own claims about what it did:
a continuity error is a write the case's earlier context forbids (a declined proposal fired again, a standing
correction not applied, another quest's card acted on, a request done twice or not at all). A case where the request
took one planner step is marked ``single_step``: it cannot show a step losing the request, and is counted apart.

An implementation module in ``impls/`` exposes ``build(meter) -> run_request``.
"""
