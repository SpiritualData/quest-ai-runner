"""ABLATION (offline arm): the overseer switched off, so the call is dropped and the run proceeds as the planner and its own
checks decide. Every scenario gets the signal ``proceed``, so the scorer counts a scenario right when ``proceed`` is among its
labelled answers and its digest shows the evidence the label turns on. Zero model calls. This is what the live arm is compared
against in ``justified_model_spend`` (the call is kept only while it gets more scenarios right than this)."""


def build(meter):  # noqa: ARG001
    return lambda digest: {"signal": "proceed", "reason": "overseer switched off", "hint": ""}
