"""BASELINE: the prompt a first draft writes, every input pasted in verbatim. Each string, list item and field of
the fixed inputs is joined in order with a blank line between, with no budget, no cut and no check for a block
that is already there, so an incident-sized input set comes out over budget and with its repeated blocks intact.
Zero model calls."""
from dataclasses import asdict, is_dataclass


def flatten(value):
    """Every piece of text in ``value``, depth first, in the order the inputs hold it."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if is_dataclass(value) and not isinstance(value, type):
        return flatten(asdict(value))
    if isinstance(value, dict):
        return [piece for item in value.values() for piece in flatten(item)]
    if isinstance(value, (list, tuple)):
        return [piece for item in value for piece in flatten(item)]
    if hasattr(value, "__dict__"):
        return flatten(vars(value))
    return [str(value)]


def build(meter):  # noqa: ARG001
    def assemble(point, inputs):  # noqa: ARG001
        return "\n\n".join(piece for piece in flatten(inputs) if piece)
    return assemble
