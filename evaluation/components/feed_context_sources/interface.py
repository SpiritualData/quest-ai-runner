"""The context-source function. Every solution is ONE function with this signature:

    feed(kind: str, world: dict) -> {"offered": [item ids], "needs_response": [item ids]}

A context source is a channel the update engine checks for what is new on a quest since an assistant last
looked: the person's notes, check-ins on goals, asks sent to people. ``world`` is a synthetic quest
(no real data): ``now`` and ``since`` (ISO times; ``since`` null is a first look), ``notes`` (``id``,
``author_kind`` user|ai|missing, ``text``, ``created_at``), ``goals`` (``id``, ``name``), ``goal_updates``
(``updateId``, ``goal``, ``note``, ``createdAt``), ``decisions`` (``decision_id``, ``status``, ``created_at``,
``resolved_at``), ``tasks`` (finished runs: ``status``, ``result``, ``worked_at``). ``kind`` names the channel
under test (``notes``, ``goal_updates``, ``decisions``, ``mixed``).

The function returns the ids of the items the channel puts in front of a run (sorted) and, of those, the ids
that are an unanswered ask. The fixed rules the cases pin: the person's own words are offered and the
assistant's own output is not echoed back as news; an answered note is not an open ask; a note nobody
attributed is never assumed to be the person's; an item seen on an earlier look is not offered again; an open
ask is offered however old up to the age and count bounds. ``expect`` (``expect.py``) compares those lists.

A different channel implementation (another source set) is another module in ``impls/`` with
``build(meter) -> feed``. No solution here calls a model; the Quest client is a fake at the network edge.
"""
