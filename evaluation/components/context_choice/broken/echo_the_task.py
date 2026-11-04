"""BASELINE: what a first draft writes. Every line answers with the request text instead of asking the member that owns it, so the context,
the resolved reference, the hits and the ranking never come from the lane's stores."""


def build(meter):  # noqa: ARG001
    def choose(case):
        return {"context": case["task"], "resolved": case["task"], "found": [case["task"]], "searched": [case["task"]]}
    return choose
