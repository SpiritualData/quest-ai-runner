"""ALTERNATIVE: the same per-step planner with the reference note switched off (``planner_abbreviate_repeat_context`` false), so
every step re-sends the transcript and context block. The comparison arm of ``rebuilt_per_step``; real model calls."""
from keep_a_working_stream.impls import rebuilt_per_step


def build(meter):
    return rebuilt_per_step.build(meter, abbreviate=False)
