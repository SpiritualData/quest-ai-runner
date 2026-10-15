"""The recipe-fast-path function. Every solution is ONE function with this signature:

    handle(message: str, world: RecipeWorld) -> dict

``world`` (``fake_world.RecipeWorld``) is five fake quest tools (three habit writes, a note, a goal, one
read) behind the real tool registry types; every invocation is recorded in ``world.calls``. The function
carries out one chat turn however it likes and returns {"reply": str, "exit_reason": str|None}; an
``exit_reason`` of "recipe" means a replayed operation answered without the normal plan and context search.

The SAME function instance sees the whole run, in order, so a solution that learns (the recipe store)
accumulates between turns exactly as production does. Before the first scored ask of a tool, the scorer
sends that tool's TEACH message once (the first-time request, planner path in every solution).

Scoring reads the world's recorded calls, never the reply text (``score.py``): repeat asks must call the
right tool with the right arguments, near-miss asks must NOT call the lookalike mutating tool, and
unrelated chat must call nothing.

An implementation module in ``impls/`` exposes ``build(meter) -> handle``.
"""
