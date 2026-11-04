"""BASELINE (``private-context-needs:send_everything``): selects every candidate, the context a system without selection would send."""


def build(meter):  # noqa: ARG001
    return lambda task, candidates: [c["item_id"] for c in candidates]
