"""The first-beat function (the metric of ``narrate-a-turn``, ``metric-narrate-a-turn``). Every solution is ONE function:

    narrate(case: dict) -> dict

``case`` is the start of one turn (``cases.json``): the person's new message, the recent conversation if any, and the lines
already spoken in earlier turns. The function returns:

  beat      the line the person would read first (the text of the first narration event), or None when nothing was said
  seconds   how long after the turn started the beat arrived (None when none did)

The real ``Narrator`` of the library runs in every arm (its prompt, its repeat detector, its dash cleanup and its emission
path). An arm decides only what stands where the line comes from: a model call (today, on the Claude Code subscription)
or a fixed line chosen from the turn's mode and first event (the floor, no model call).

Scoring (``score.py``): a beat counts when it passes the prompt's own contract (one short first-person sentence, no dash, no
markdown, no figure the turn does not hold), names something specific from the person's message (the metric's specificity
floor, because a bland line is always supported), and a grader finds every claim in it backed by what the turn has at that
moment, which is the message and the recent conversation and nothing read yet. A turn whose earlier lines already said the
generic thing is right to stay quiet. An implementation module in ``impls/`` exposes ``build(meter) -> narrate``.
"""
