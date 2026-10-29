"""The end-to-end turn function (the metric of ``answer-end-to-end``). Every solution is ONE function:

    turn(case: dict) -> dict

A case is one message a person sends QAR, the small world it is sent into (quests, notes, goals, a pinned quest or none, a
canned web) and what a right turn looks like. The function runs the WHOLE path (route, context, plan, act, answer) and returns
the facts the scorer reads:

  reply       the text the person would read
  writes      every mutating tool call that landed in the world, in order: {"tool", "args", "quest_id"}
  reads       the read-only tool calls that ran
  web         the web queries the turn made
  route       what the turn did: ``answer`` (replied from what it had or read), ``act`` (changed the world), ``web``,
              ``queue`` (queued follow-up work), ``deep`` (asked for a deep run), ``confirm`` (held for a person), ``clarify``
  terminal    for a ``terminal`` case: {"stdout", "exit"} of the ``send`` command

The real orchestrator and the real tool catalogue run in every model arm; an arm decides only what stands at the model
boundary (a scripted ideal planner, a rule floor, or a live model on the Claude Code subscription). Scoring is by the WORLD
afterwards (``score.py``), never by a model judge, so scoring costs no model calls. An implementation module in ``impls/``
exposes ``build(meter) -> turn``.
"""
