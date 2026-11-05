"""ALTERNATIVE: the lexical top few, no vector search and no model (``core.spec_selection.lexical_only``). Zero model calls."""
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import spec_selection
from spec_selection import arms


def build(meter):
    return arms.runner(spec_selection.lexical_only, meter)
