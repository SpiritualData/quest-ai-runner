"""The card-cache function. Every solution is ONE function with this signature:

    scenario(kind: str, args: dict) -> dict

The card repository (``QuestApiCardRepository``) takes a ``CardCache`` seam so a lane can keep working
when the card backend is unreachable. The contract: whatever cache is plugged in, the repository's
behaviour toward its caller is the same (the same cards come back, a failed write still returns False and
the outage stays loud); and a cache that stores things gets them: the listing after every refresh, every
failed write offered in order, and a cold start with the backend down can be served from the stored
listing. The fake is the backend: the in-process Quest card API from ``tests/quest_card_backend_fake.py``
(a local HTTP server) that can be switched off.

Kind ``scenario``, args {steps}: each step is one of
  ["put", id]     the backend gains a card          ["load"]       ``load_all()`` on the repository
  ["write", id]   ``write(id, card)``               ["down"]       the backend stops answering
  ["new_repo"]    a fresh repository (a new process) sharing the same cache object
Returns {"cards": sorted ids of the last load, "write_ok": result of the last write or None,
"cache_stored": cards in the cache's stored listing, "cache_pending": writes the cache holds,
"loud": the repository reports itself degraded with a counted error}.

An implementation module in ``impls/`` exposes ``build(meter) -> scenario``. No solution here calls a model.
"""
