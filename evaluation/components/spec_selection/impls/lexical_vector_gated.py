"""SELECTED: lexical plus a real local vector search, then a confidence gate (``core.spec_selection.lexical_vector_gated``): clear
and empty requests are answered with no model call, every other request costs ONE real ``claude`` CLI call (the runner's
``ClaudeCliProvider``) that judges the best candidates together. No scripted stand-in; needs a logged-in ``claude`` and the cached
embedder."""
from quest_ai_runner.core import spec_selection
from spec_selection import arms

PAID = True   # real model calls (subscription-metered): `esoi run --stale` asks before running it


def build(meter):
    return arms.runner(spec_selection.lexical_vector_gated, meter, judge=True, vector=True)
