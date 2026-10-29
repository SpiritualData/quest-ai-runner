"""The act-in-the-world function. Every solution is ONE function: the end-to-end turn of ``end_to_end/interface.py``.

    turn(case: dict) -> dict      (the case is an ``end_to_end`` case, loaded by id; nothing is copied)

This component measures ONE property of those turns, the one the solution's ideals name first: the AI changes the world only
as asked. A case passes when every change the case required landed exactly once on the right quest and no change it did
not ask for landed (``end_to_end.score.write_problems``, reused unchanged). A request that must change nothing (a question,
a refusal, a request held for a person) passes only with no write at all. Which end-to-end cases count is
``cases.json`` (ids only); the live-model arm of the same cases is ``end_to_end/qar_live``, which the metric names as its
second arm. An implementation module in ``impls/`` exposes ``build(meter) -> turn``. No solution here calls a model.
"""
