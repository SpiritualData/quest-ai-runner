"""ABLATION (built, was proposed): the registry plus a per-call ablation against the owner metric. A prompt passes when it is
registered, its owner arm's latest row is current, its call beats the arm with the call dropped or downgraded on the same
cases, and its maximum dollars per turn and seconds per call stay inside its own budget. Reads the traces ``measure.py``
kept (eval traces, labelled so); makes no model call itself."""
from justified_model_spend import score


def build(meter):  # noqa: ARG001
    return lambda case: score.judge(case)
