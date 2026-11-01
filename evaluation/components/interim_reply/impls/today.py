"""BASELINE (``reply-in-steps:today`` and ``ask-on-block:today``): the behaviour before the incremental-replies build. No interim message
is sent for an unmet attempt (the only interim needed done evidence), and a block on a person ends as text, not a filed ask. Zero model
calls. A reconstruction of the earlier behaviour for comparison, not code that runs today."""


def build(meter):  # noqa: ARG001
    def run(kind, args):
        if kind == "turn":
            return {"sent": []}
        asker = str(args.get("asker") or "")
        return {"ask": False, "assignee": asker, "asked_of": "asker", "same_key": True}
    return run
