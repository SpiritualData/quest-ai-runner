"""The classification function. Every solution is ONE function with this signature:

    classify(message: str, context: dict) -> dict

``context`` = {"quest": {"title", "outcome"}, "tools": [tool names offered, e.g. "create_quest_goal"]}
Returns {"route": "answer" | "tool" | "deep", "tool": <tool name or None>}.

  answer : reply inline from what is known (including a read of the quest's own data)
  tool   : one direct standard-tool call (create a goal, set a field, find experiments)
  deep   : needs a machine: code, files, a long research job, anything outside the quest database

An implementation module in ``impls/`` exposes ``build(meter) -> classify``; it counts any model call
it makes with ``meter.record()`` (``meter.wrap(provider)`` does it for a QAR provider). The harness
(``../run.py``) and scorer (``score.py``) never change when a solution is added.
"""
ROUTES = ("answer", "tool", "deep")
