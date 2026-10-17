"""The conversation-recall function. Every solution is ONE function with this signature:

    recall(query: str, user: str, world: dict) -> {"ids": [conversation id, ...]}

``world`` (``conversations.json``) is a set of SYNTHETIC past conversations (invented) belonging to two users.
A solution returns the ids of the past conversations it would hand the planner for ``query`` on behalf of
``user``, best first (at most 3). Scoring (``score.py``): every labelled relevant conversation is returned, and
no conversation of another user is (a leak). No model, no network.

An implementation module in ``impls/`` exposes ``build(meter) -> recall``; these make no model call."""
MAX_CONVS = 3
