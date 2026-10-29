"""CURRENT, live: the same helper calls with a real model behind them. PAID: it spends. The model is pinned by
``QAR_EVAL_LIVE_MODEL`` (default claude-haiku-5-5, on the Anthropic API; a Gemini id is a candidate, never the default). Stay inside the staged ladder of esoi/metric-run-policy.md
and always pass ``--max-calls``. The stub answers some cases name (a provider that raises, an empty answer) are
offline scenarios a real model cannot be made to produce, so run the live arm with ``--only`` over cases marked live."""
PAID = True
import os

from assist_a_terminal_session import arms
from quest_ai_runner import cli_quest

MODEL_ENV = "QAR_EVAL_LIVE_MODEL"
DEFAULT_MODEL = "claude-haiku-5-5"


def build(meter):
    import live_arm
    model = os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    provider = meter.wrap(live_arm.api_provider(model))
    meter.model = model

    def assist(case):
        kind = case["kind"]
        if kind == "ack":
            with_model = arms.PinnedModel(provider, model)
            return arms.run_ack(case, lambda: with_model, cli_quest._cmd_send)
        classify, judge = arms.helpers_from(arms.PinnedModel(provider, model), model)
        if kind == "classify":
            return arms.run_classify(case, classify)
        if kind == "judge":
            return arms.run_judge(case, judge)
        return arms.run_gate(case, classify)
    return assist
