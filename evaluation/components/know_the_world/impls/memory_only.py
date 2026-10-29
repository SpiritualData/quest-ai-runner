"""FLOOR: no research. The answer is stated from what the model remembers, with no source, whether or not any page supports it.
It is the claim the case labels, stated flat. Zero model calls."""


def build(meter):  # noqa: ARG001
    return lambda case: {"text": case["memory_answer"], "cited": [], "retrieved": []}
