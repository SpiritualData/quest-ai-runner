"""CURRENT, live on the fast tier the shipped configuration pins (``draft-a-metric:capped_model_draft``, priced): the same draft turn over
a Gemini flash-lite model, so the dollars, tokens and seconds are what a fast-tier call costs. The module's tier is the caller's, so this
is the cheapest tier a caller can pick and a lower bound on a stronger one. API priced (fractions of a cent per case): one case, then the
set, with ``--max-calls``."""
PAID = True
from quest_ai_runner.runner import metric_draft
from draft_a_metric import arms
import live_arm


def build(meter):
    assert metric_draft.MAX_MODEL_CALLS == 1   # the cap the module states is the one the scorer checks
    provider, model = live_arm.build(meter, "fast_api")
    return lambda case: arms.run_draft(case, provider, model)
