"""CURRENT, live: the same helper calls with a real model behind them. PAID: it spends. The provider is built from
``QAR_EVAL_PROVIDER`` (default gemini) and the tier model comes from the registry's fast tier, pinned by
``QAR_EVAL_LIVE_MODEL`` (default gemini-3.1-flash-lite). Stay inside the staged ladder of esoi/metric-run-policy.md
and always pass ``--max-calls``. The stub answers some cases name (a provider that raises, an empty answer) are
offline scenarios a real model cannot be made to produce, so run the live arm with ``--only`` over cases marked live."""
PAID = True
import os

from assist_a_terminal_session import arms
from quest_ai_runner import cli_quest

MODEL_ENV = "QAR_EVAL_LIVE_MODEL"
DEFAULT_MODEL = "gemini-3.1-flash-lite"


def build(meter):
    from quest_ai_runner.adapters.gemini_provider import GeminiProvider
    model = os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    provider = meter.wrap(GeminiProvider())
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
