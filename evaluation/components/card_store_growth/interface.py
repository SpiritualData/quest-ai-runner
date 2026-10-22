"""The card-store-growth function. Every solution is ONE function with this signature:

    grow(kind: str, args: dict) -> dict

It measures whether a card store stays proportionate to its corpus when it is BUILT and USED, not only when it is
cleaned after the fact (that is ``card_growth``). Each kind builds nested corpora with a store beside each, in a
temporary folder, with a fake model that never leaves the process, and returns what it found:

  fuzz      args {seed, steps}: plays a seeded random sequence (index, edit files, add and delete folders, learn
            cards, write a quest card, merge stores, widen a root, a cards folder inside its corpus, a store
            opened through a symlink, a small card limit) and checks after every step what must hold
            (``world.py``: I1 to I7). Returns {violations: count, first: the first one}.
  legacy    args {with_provenance}: a healthy two-store world, plus the copies an older version left behind
            under stacked prefixes (marked as imports or not); the store is opened and refreshed once, as a
            runner starts. Returns {clean, before, after, excess, kept_on_disk}: ``excess`` is the cards left
            beyond what the corpus calls for, ``kept_on_disk`` that nothing was deleted.
  limit     args {folders, limit, passes}: a corpus of more folders than the limit, indexed for several passes.
            Returns {cards, at_or_below_limit, notice_with_breakdown, refused}.
  limit_learned  args {limit}: a full store learns three cards from runs. Returns {total_at_limit, nothing_deleted}.
  limit_open     args {cards, limit}: a store opened above its limit. Returns {notice_with_breakdown, nothing_removed}.
  limit_protected args {limit}: a full store whose topic cards were all taught something learns three cards.
            Returns {moved_that_hold_something, notes_kept, learned_admitted}.
  tidy_protected  a tidy over cards pinning one topic's files, some written by hand, taught, or tagged for a quest.
            Returns {protected_moved, by_hand_kept, tagged_kept, taught_kept, copy_note_kept, plain_copy_moved}.
  stale_writer    an updater shown a card another process has since moved out. Returns {stub_created, ...}.
  mounted_edit    an edit meant for a card read in place from a neighbour. Returns {own_card_untouched, ...}.
  old_copy_with_notes  an older version's copy, taught something, that its replacement now stands for. Returns
            {retired, notes_joined, tags_joined, usage_joined}.
  moved_corpus    the corpus folder renamed with its store inside. Returns {indexed_after_rename}.
  fresh_bootstrap_cost  args {folders, limit}: model calls for a corpus with more folders than room.
            Returns {calls, refused, second_pass_free, ...}.
  concurrent_tidy args {cards}: four processes tidy one store at once. Returns {files_conserved,
            moves_counted_once, notes_folded_once}.
  concurrent_limit  args {limit}: four processes learn cards in a store near its limit. Returns {limit_held,
            all_written, moves_counted_once, nothing_deleted}.

An implementation module in ``impls/`` exposes ``build(meter) -> grow``. No solution here calls a model; ``grow``
also runs against an older checkout of the library, which is how the "before" number is taken.
"""
