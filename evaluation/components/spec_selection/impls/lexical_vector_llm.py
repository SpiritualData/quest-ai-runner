"""CURRENT: lexical TF-IDF, optional vector search, parallel batched model judging (``core.spec_selection``), with a
scripted judge. Zero real model calls."""
from quest_ai_runner.core import spec_selection
from spec_selection import arms

SIMULATED = True   # the judge is scripted: it meters the calls a real model would get and spends nothing


def build(meter):
    return arms.runner(spec_selection.lexical_vector_llm, meter)
