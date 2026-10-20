"""CHEAP VARIANT: lexical plus a real local vector search and the confidence gate, with NO model (``lexical_vector_gated`` given no
provider): clear and empty requests are answered as in the gated arm, ambiguous ones return the best five fused candidates.
Free; shows what the model call in the gated arm adds."""
from quest_ai_runner.core import spec_selection
from spec_selection import arms


def build(meter):
    return arms.runner(spec_selection.lexical_vector_gated, meter, judge=False, vector=True)
