"""FLOOR: no task is mapped to a measure, as before this solution existed. Every task is reported as not measured. Zero model calls."""


def build(meter):  # noqa: ARG001
    return lambda case: {"measured": False, "rate": None, "why": "nothing maps tasks to measures"}
