"""The recent-context function. Every solution is ONE function with this signature:

    recall(case: dict) -> {"ids": [card id, ...]}

A case is a short SYNTHETIC history: earlier turns (each with the scope keys it was recorded under, the user's
text and the cards that turn selected), then a NEW input with the scope keys it is loaded under and whether it
is a follow-up. A solution returns the ids of the remembered cards it would warm-start the new turn with, best
first. Scoring (``score.py``): every labelled relevant card is returned and no forbidden one is (a stale card
dragged into an unrelated question, or a card from a different quest). No model, no network.

An implementation module in ``impls/`` exposes ``build(meter) -> recall``; these make no model call."""
