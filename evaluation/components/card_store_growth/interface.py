"""The card-store-growth function. Every solution is ONE function with this signature:

    grow(kind: str, args: dict) -> dict

It measures whether a card store stays proportionate to its corpus when it is BUILT and USED, not only when it is
cleaned after the fact (that is ``card_growth``). Each kind builds nested corpora with a store beside each, in a
temporary folder, with a fake model that never leaves the process, and returns what it found:

  fuzz      args {seed, steps}: plays a seeded random sequence (index, edit files, add and delete folders, learn
            cards, write a quest card, merge stores, widen a root, a cards folder inside its corpus, a store
            opened through a symlink, a small card limit) and checks after every step what must hold
            (``world.py``: I1 to I6). Returns {violations: count, first: the first one}.
  legacy    args {with_provenance}: a healthy two-store world, plus the copies an older version left behind
            under stacked prefixes (marked as imports or not); the store is opened and refreshed once, as a
            runner starts. Returns {clean, before, after, excess, kept_on_disk}: ``excess`` is the cards left
            beyond what the corpus calls for, ``kept_on_disk`` that nothing was deleted.
  limit     args {folders, limit, passes}: a corpus of more folders than the limit, indexed for several passes.
            Returns {cards, at_or_below_limit, notice_with_breakdown, refused}.
  limit_learned  args {limit}: a full store learns three cards from runs. Returns {total_at_limit, nothing_deleted}.
  limit_open     args {cards, limit}: a store opened above its limit. Returns {notice_with_breakdown, nothing_removed}.

An implementation module in ``impls/`` exposes ``build(meter) -> grow``. No solution here calls a model; ``grow``
also runs against an older checkout of the library, which is how the "before" number is taken.
"""
