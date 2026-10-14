"""The quest-operations function. Every solution is ONE function with this signature:

    run_operation(message: str, world: FakeQuest) -> dict

``world`` is an in-memory quest (``fake_quest.FakeQuest``) whose ``world.registry`` is the standard QAR
tool catalog (same names, descriptions and argument schemas as production) with handlers that mutate
the in-memory state instead of calling a Quest server. The function does whatever it likes (QAR's
whole orchestrator, a single direct tool call) to carry out the request, and returns {"reply": str}.

Scoring (``score.py``) reads the WORLD afterwards, not the reply: did the right change land, and did
nothing else change. No model judge, so scoring costs no model calls.

An implementation module in ``impls/`` exposes ``build(meter) -> run_operation``.
"""
