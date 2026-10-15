"""The card-quality function. Every solution is ONE function with this signature:

    select(query: str, world: dict) -> {"view": str, "titles": [str]}

``world`` (``world.json``) is what the system has learned from three finished deep tasks: for each, the request, a
one-line summary of what was done, and the out-of-band "future context" bullets that name the user's collections
(each with an id). A solution learns cards from that world however it likes (a model-driven updater, a keyword
extractor), then, for a NEW request, returns the context view the planner would receive after the store has selected
cards and RESOLVED their references fresh. ``titles`` lists the cards selected.

Scoring is on the view, not on the reply (``score.py``): the labelled collection's id must appear in the view (the
right card was selected and its reference resolved live for this query), and an unrelated query must pull in none of
the learned collections (no leak). Extra, unrelated collections resolved alongside the right one are counted, not
failed (a card can legitimately carry two collections).

Two arms:

* ``seeded_keyword_store`` (offline, recorded): cards are written from the world by a deterministic extractor (no
  model) and read back through the real ``FileContextStore.assemble`` with a stub collection resolver. This measures
  selection and fresh resolution of the persisted-card path; it does NOT measure the learning step, which is the
  model's part.
* ``live_updater_hybrid`` (needs a model, Voyage and an embedded Qdrant; waived in waivers.csv S10): the original
  ``card_quality_eval.py`` flow: the real async updater learns the cards, a Qdrant card repository plus a Voyage query
  vector arm are fused with the keyword arm (the backend recipe).

An implementation module in ``impls/`` exposes ``build(meter) -> select``.
"""
