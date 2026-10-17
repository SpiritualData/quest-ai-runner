"""The classification function. Every solution is ONE function with this signature:

    classify(message: str, context: dict) -> dict

``context`` = {"quest": {"title", "outcome"}, "tools": [tool names offered, e.g. "create_quest_goal"]}
Returns {"route": ..., "tool": <first tool name or None>, "calls": [{"name", "args"}, ...]}.

  answer  : reply inline from what is known (including a read of the quest's own data)
  tool    : direct standard-tool calls (create a goal, set a field, find experiments)
  deep    : needs a machine: code, files, a long research job, anything outside the quest database
  confirm : a person must approve first (an irreversible, destructive or outward act)
  clarify : the request cannot be acted on yet (it points at something unnamed)

The metric scores the router's WHOLE job, not only the label: the route, the tool, the arguments it
filled in, every tool a multi-step request needs, no confirming argument on a first destructive ask,
and no tool call where the message asked for no change (see ``score.py`` and the case fields).

An implementation module in ``impls/`` exposes ``build(meter) -> classify``; it counts any model call
it makes with ``meter.record()`` (``meter.wrap(provider)`` does it for a QAR provider). The harness
(``esoi run``) and scorer (``score.py``) never change when a solution is added.
"""
ROUTES = ("answer", "tool", "deep", "confirm", "clarify")
