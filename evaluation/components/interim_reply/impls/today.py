"""ALTERNATIVE (``reply-in-steps:today`` and ``ask-on-block:today``): the behaviour before the incremental-replies build. No interim message
is sent for an unmet attempt (the only interim needed done evidence), and a block on a person ends as text, not a filed ask. Zero model
calls offline. A reconstruction of the earlier behaviour for comparison, not code that runs today. Its REAL cases (kind ``real``, run only with
QAR_EVAL_LIVE_TURNS=1) run the real orchestrator with the interim switch off, the behaviour that ships today."""


def real_turn(turn, switched_on):
    """A REAL turn (kind ``real``, only with QAR_EVAL_LIVE_TURNS=1): the orchestrator on the Claude Code subscription with the
    interim switch ``switched_on``; the structural checks of ``live_turns.check`` and the work-trace check give ``problems``."""
    from interim_reply import live_turns
    got = live_turns.run_in_subprocess(turn, switched_on)
    return {"problems": list(got.get("problems") or []) + list(got.get("work_trace_problems") or []),
            "interims": sum(m.get("kind") == "interim" for m in got.get("messages") or []),
            "asks": len(got.get("asks_filed") or [])}


def build(meter):  # noqa: ARG001
    def run(kind, args):
        if kind == "text":
            return {"sent": False, "leaks": False, "has_receipt": False}   # no interim is sent at all
        if kind == "real":
            return real_turn(args["turn"], False)
        if kind == "turn":
            return {"sent": []}
        asker = str(args.get("asker") or "")
        return {"ask": False, "assignee": asker, "asked_of": "asker", "same_key": True}
    return run
