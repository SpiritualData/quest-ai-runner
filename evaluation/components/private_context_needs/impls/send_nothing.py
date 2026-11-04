"""FLOOR (``private-context-needs:send_nothing``): selects no context at all. Right for self-contained inputs, a miss for every other."""


def build(meter):  # noqa: ARG001
    return lambda task, candidates: []
