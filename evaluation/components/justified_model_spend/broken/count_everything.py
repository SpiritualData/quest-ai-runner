"""BASELINE: the shortcut a first draft would write. Every call is justified because it exists: it reads no trace, makes
no ablation and checks no budget, and weighs every prompt equally."""


def build(meter):  # noqa: ARG001
    return lambda case: dict(verdict="pass", reasons=[], stats=None, avg_spend=1.0)
