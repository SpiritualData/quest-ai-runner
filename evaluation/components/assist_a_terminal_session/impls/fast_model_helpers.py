"""CURRENT, offline: the fast-tier helper calls as the terminal makes them (``cli_quest._cmd_send``,
``cli_config._ask_classifier`` and ``_goal_judge``), with the model stubbed at the provider boundary by a REFERENCE
model: it answers each case with the label replayed (or the stub answer the case names). So the run is $0 and
deterministic, and it scores the code around the call (the printed line, the fallbacks, the checks on the answer),
a ceiling for the model route. What a real model answers is measured by ``fast_model_helpers_live``."""
SIMULATED = True   # meters a stand-in for the model: recorded as model 'simulated' at cost 0.0, compared on calls
from assist_a_terminal_session import arms
from quest_ai_runner import cli_quest


def build(meter):
    def assist(case):
        provider = meter.wrap(arms.ReplayProvider(lambda messages: arms.reference_answer(case)))
        kind = case["kind"]
        if kind == "ack":
            return arms.run_ack(case, lambda: provider, cli_quest._cmd_send)
        classify, judge = arms.helpers_from(provider)
        if kind == "classify":
            return arms.run_classify(case, classify)
        if kind == "judge":
            return arms.run_judge(case, judge)
        return arms.run_gate(case, classify)
    return assist
