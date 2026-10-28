"""CURRENT: the registry alone. A prompt counts as justified when it is a registered spec with a trace of its owner arm,
which is all the registry and its enforcement test can say: there is no ablation and no budget. Reads the same traces;
no model call."""
from justified_model_spend import score


def build(meter):  # noqa: ARG001
    return lambda case: score.judge(case, ablate=False, check_budget=False, check_owner=False)
