"""The Quest ordering function. Every arm is ONE function with this signature:

    order(kind, args) -> list of ids

``kind`` is the family of the case (``quest-list``, ``work-order``, ``goal-ladder``, ``open-asks`` for Quest's own cases; ``rank``, ``carried``,
``stated`` for the hub's) and ``args`` is the input of the hub's Prioritization: ``person``, ``candidates`` (mappings with ``id``, ``kind`` and the fields
the data carries, Quest's ``rank_by_person``, ``owner`` and ``horizon`` among them), ``now``, the ``subject`` of the call (``work``, ``asks`` or ``goals``, as the hub's Prioritization names them), optionally a ``batch_limit``, and, when the person said something, ``stated``. It returns the
ids of the candidates in the order the arm puts them. The hub's cases and Quest's are answered by the same function.

An implementation module in ``impls/`` exposes ``build(meter) -> order``.
"""
