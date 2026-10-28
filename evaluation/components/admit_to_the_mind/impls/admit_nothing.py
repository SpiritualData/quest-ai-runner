"""BASELINE: the floor. Nothing is admitted to the next step's view."""


def build(meter):  # noqa: ARG001
    def admit(case):  # noqa: ARG001
        return {"view": "[]"}
    return admit
