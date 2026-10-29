"""BASELINE: every message is saved as a note on the first quest and the reply says Done (``end_to_end/broken/act_on_everything``).
Every request that must change nothing, and every one that asked for a different change, fails it."""
from end_to_end.broken import act_on_everything


def build(meter):
    return act_on_everything.build(meter)
