"""The card-growth function. Every solution is ONE function with this signature:

    settle(cards_dir: Path) -> None

``cards_dir`` is a card store (one ``<id>.json`` per card, the layout of ``FilesystemCardRepository``) that a
lane in copy mode left behind after ``passes`` bootstrap passes over a corpus whose root holds a second store
nested inside it (``fixtures.py``). The function acts on the store in place, the way an operator or a guard
would, and must leave one card per distinct source-file group: no duplicate copy of a card, no group lost, and
no card another system owns or a person taught removed. It moves cards out of the index; it never needs to
delete one.

An implementation module in ``impls/`` exposes ``build(meter) -> settle``. No solution here calls a model.
"""
