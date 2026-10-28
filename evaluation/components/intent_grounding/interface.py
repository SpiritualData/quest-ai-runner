"""The intent-grounding function (the metric of ``ground-in-intent``). Every solution is ONE function:

    answer(case: dict) -> dict

``case`` is one chat turn on one quest (``cases.json``): the person's message, the facts of the quest, the intent frame the
server would have computed for it (empty for a quest with no intent above it), and the same fields the chat_quality
datasets use to judge a reply (a rubric, pivot facts the answer must draw on, deterministic reply checks). The returned
record is the one ``chat_quality`` produces: ``passed`` (bool), ``score`` (0..1 or None), ``precheck`` (hard failures found
by code), ``judged`` (the model judge's verdict), ``error``.

Two kinds of case, told apart by ``kind``:

  intent   the right answer depends on the intent the quest serves (which work closes a gap, a request that conflicts with an
           ideal, a gap under the intent, a plan that cannot reach the vision, which experiment to run). The frame carries
           the intent; a reply without it has nothing to ground on.
  control  the right answer depends on the quest's own facts, which every arm carries, and the frame is empty, so the
           prompt of every arm is the same bytes. The pass rate of a control must not move with the arm.

The arm decides one thing: whether the intent frame block is in the prompt. The reply model, the prompt around it and the
judge (the one ``chat_quality`` uses, ``claude -p`` on the subscription) are the same for every arm. An implementation
module in ``impls/`` exposes ``build(meter) -> answer``.
"""
