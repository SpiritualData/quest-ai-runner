"""CURRENT: lexical TF-IDF plus a real local vector search, fused, then the real cheap model judging the candidates in
parallel batches (``core.spec_selection``). Every call is a real ``claude`` CLI call (the runner's ``ClaudeCliProvider``) and
the vector search embeds with bge-small; there is no scripted stand-in. Needs a logged-in ``claude`` and the cached embedder."""
from quest_ai_runner.core import spec_selection
from spec_selection import arms

PAID = True   # real model calls (subscription-metered): `esoi run --stale` asks before running it


def build(meter):
    return arms.runner(spec_selection.lexical_vector_llm, meter, judge=True, vector=True)
