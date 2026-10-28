"""CURRENT, live on the Claude Code subscription (``draft-a-metric:capped_model_draft``): the shipped ``MetricDraft`` turn, one
structured `plan` call to `claude -p` on its cheapest tier. No API dollars; model calls are counted. Stay on the ladder and always
pass ``--max-calls``."""
PAID = True
from quest_ai_runner.runner import metric_draft
from draft_a_metric import arms
import live_arm


def build(meter):
    assert metric_draft.MAX_MODEL_CALLS == 1   # the cap the module states is the one the scorer checks
    provider, model = live_arm.build(meter, "subscription")
    return lambda case: arms.run_draft(case, provider, model)
