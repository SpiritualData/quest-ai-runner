"""FLOOR (``draft-a-metric:ask_owner``): no model call. No draft is made, so no value is proposed and the owner is asked for the metric
in their own words. Right on every case whose evidence is too thin for a value, and wrong on every case a draft could have served."""
from draft_a_metric import arms


def build(meter):  # noqa: ARG001
    return lambda case: arms.run_draft(case, None)
