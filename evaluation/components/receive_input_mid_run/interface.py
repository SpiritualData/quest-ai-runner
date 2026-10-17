"""The mid-run input function. Every solution is ONE function with this signature:

    receive(kind: str, args: dict) -> dict

The user keeps typing while a run is working. The inbox is where an interface drops those messages, and the
orchestrator picks them up between steps so the next decision sees them. Two kinds:

  run        args {reads, pushes, conv}  -> {"first_seen": {text: planner call index | null},
                                             "copies": {text: occurrences in the last planner prompt},
                                             "lost": [texts neither seen by the planner nor still pending],
                                             "leaked": [other conversations' texts the planner saw]}
             A real Orchestrator (scripted model, no sources, no network) takes ``reads`` read steps and then
             answers, on conversation ``conv``. ``pushes`` is [{"at": -1 | planner call index, "conv", "text"}]:
             the message is pushed to the wired inbox before the run (-1) or while that planner call is in
             flight. The metric is delivery: the message reaches the next decision, once, to the right
             conversation, and is never silently lost.
  inbox_ops  args {ops}                  -> {"results": [...]}
             Direct calls on the inbox port: ["push", conv, text], ["drain", conv], ["peek", conv]; each
             returns its result (drain and peek the list, push null). Pins the port's contract: first in first
             out, drain clears, peek does not, conversations never mix, blank messages are ignored.

An implementation in ``impls/`` exposes ``build(meter) -> receive`` and passes ITS inbox (or none) to the
shared driver in ``driver.py``; the orchestrator side is always the live code. No model calls.
"""
