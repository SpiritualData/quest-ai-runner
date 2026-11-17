"""BASELINE: no check, every text goes out as written. The floor the real guard must beat. Zero model calls."""


def build(meter):  # noqa: ARG001
    def check(kind, args):
        if kind != "outward":
            raise ValueError(kind)
        return {"refused": False, "named": []}
    return check
