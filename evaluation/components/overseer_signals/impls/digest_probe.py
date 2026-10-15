"""FLOOR (offline arm): makes no judgement (returns ``signal: None``), so the scorer measures only whether the digest
shows the overseer the evidence each label turns on. Zero model calls; this is the arm that can be recorded
on a host with no model."""


def build(meter):  # noqa: ARG001
    return lambda digest: {"signal": None, "reason": "", "hint": ""}
