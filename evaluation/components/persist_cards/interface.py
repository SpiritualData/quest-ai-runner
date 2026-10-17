"""The card-persistence function. Every solution is ONE function with this signature:

    persist(kind: str, args: dict) -> dict

A context card is stored through the ``CardRepository`` port. The ideal of the persist-cards solution is "no lost
or duplicated cards, and the same behaviour on every backend", so this component does not rank backends by a
score: it runs ONE conformance case set against each backend and a backend either keeps the contract or it does
not. Each kind is one scenario against a fresh store, and the dict returned is what the scenario observed:

  roundtrip     cards written come back field for field from ``read`` and ``load_all``
  overwrite     rewriting an id keeps one card with the newest content (no duplicate, no stale copy)
  no_loss       N distinct cards written, N distinct cards loaded, every id present (``args.n``)
  no_duplicate  the same card written repeatedly is still one card
  delete        a deleted card is gone from ``read``, ``exists``, ``load_all`` and after a restart; its
                neighbours stay; deleting a missing card is not an error
  concurrent    several threads write distinct ids and one shared id; nothing is lost and the shared card is
                one whole version
  restart       a second repository object on the same storage sees every card the first wrote
  revision      the change stamp differs after a write and after a delete
  tolerance     absent reads degrade (``None``, ``False``, ``{}``) and never raise

An implementation module in ``impls/`` exposes ``build(meter) -> persist`` and drives a LIVE repository class
(``backends.py`` holds the harness for each). No call here uses a model.
"""
