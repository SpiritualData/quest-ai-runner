"""FLOOR: the standing instruction alone. The AI is asked to say what the metric was before and after; no code reads, records or
checks it, so nothing is on record for any request. Zero model calls."""


def build(meter):  # noqa: ARG001
    return lambda case: {"rows": [], "missing": False, "proposed": False, "response": ""}
