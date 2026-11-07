"""The quest placement function (the metric of ``place-on-a-quest``). Every arm is ONE function:

    place(case: dict) -> {"picked": [quest ids], "ranked": [quest ids, best first], "calls": int}

A case carries a ``message``, the ``previous`` message of the conversation (or ""), the account's quests (``account.QUESTS``) and the labelled
``expect`` (the quest ids the message belongs to; an empty list means none fits and the right answer is to pick none). ``picked`` is what the
arm places the message on; ``ranked`` is every candidate it considered, best first; ``calls`` is the number of model calls it made.

An implementation module in ``impls/`` exposes ``build(meter) -> place``. An arm that calls a model sets ``PAID = True`` and goes through
``live_arm.build``; the others call no model, so the first stage of the cascade runs them for nothing.
"""
