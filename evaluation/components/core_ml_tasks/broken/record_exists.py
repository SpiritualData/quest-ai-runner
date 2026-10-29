"""BASELINE: what a first draft writes. A task counts as measured because its implementation is written down in the records and
the component it names exists, whether or not any labelled case or recorded row backs it. It overclaims."""
import common


def build(meter):  # noqa: ARG001
    return lambda case: {"measured": True, "rate": None, "why": "listed in the records"}
