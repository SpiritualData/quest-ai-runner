"""BASELINE (``reply-in-steps:every_attempt``): the shortcut a first draft would write. Every attempt that did not meet the goal sends an
interim, and any verdict that carries a question files an ask. Zero model calls; the delta, spacing, cap and ask-iff-blocked cases reject it."""


def build(meter):  # noqa: ARG001
    def run(kind, args):
        if kind == "turn":
            return {"sent": [i for i, s in enumerate(args["steps"]) if not s["met"]]}
        asker = str(args.get("asker") or "")
        return {"ask": bool(str(args.get("question") or "").strip()), "assignee": asker, "asked_of": "asker", "same_key": False}
    return run
