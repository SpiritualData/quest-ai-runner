"""The proactive-guidance function (the metric of ``guide-toward-the-long-term-goal``). Every solution is ONE function:

    reply(case: dict) -> {"reply": str, "provider": <a metered model provider for the judge>}

``case`` is one chat turn (``cases.json``): the labelled context the assistant holds (where the quest sits in the intent hierarchy,
the quest, the next milestone, the current metric reading; for a representative, the quests it looks after), the earlier lines of
the conversation if any, and the person's message. Two classes, told apart by ``class``:

  nudge      the moment is natural (the request is about this work, or the person is idle or closing): a short pointer toward the
             next milestone is wanted, at the intent level the case names (``expected_level``).
  restraint  the moment is not (urgent, narrow, unrelated, emotional, or the milestone was already raised or the person asked to stop):
             any pointer is the failure, and the request must simply be answered.

``level`` is the intent level of the context the assistant was handed: ``quest`` (one quest and its milestone), ``goal`` (a goal in
focus inside a quest, with its own next target) or ``rep`` (a representative's portfolio of several quests under one intent).

Scoring (``score.py``) is two sided. Checks by code first (no dash, short, a nudge names a real milestone and a real reading from the
case and no figure the case does not hold, the answer comes before the nudge, a restraint reply names neither), then one grader call
(``claude_judge``) for the rest: answers the request, nudge at the right level, not awkward. Reported per class and per level.

An implementation module in ``impls/`` exposes ``build(meter) -> reply``.
"""
