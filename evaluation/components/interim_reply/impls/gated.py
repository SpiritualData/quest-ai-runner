"""CURRENT (``reply-in-steps:gated`` and ``ask-on-block:structural``): the real ``core/interim_policy.py`` and ``core/ask_policy.py``. The turn
loop here is the orchestrator's own: one ``InterimLedger`` per turn, ``decide`` before each emit, ``record`` after a send. Zero model calls offline. Its REAL cases (kind ``real``, run only with QAR_EVAL_LIVE_TURNS=1) run the real
orchestrator with ``interim_replies`` on."""
from types import SimpleNamespace

from quest_ai_runner.core import ask_policy, interim_policy


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
        if kind == "real":
            return real_turn(args["turn"], True)
        if kind == "turn":
            ledger = interim_policy.InterimLedger()
            sent = []
            for i, step in enumerate(args["steps"]):
                state = interim_policy.InterimState(
                    enabled=True, met=step["met"], blocker=step["blocker"], loop_runs_again=step["loop_runs_again"], ask_open=step["ask_open"],
                    has_why=step["has_why"], has_content=step["has_content"], sent_count=ledger.sent_count, last_sent_at=ledger.last_sent_at,
                    now=float(step["at"]), signature=(step["signature"],), last_signature=ledger.last_signature)
                if interim_policy.decide(state).send:
                    ledger.record(float(step["at"]), state.signature)
                    sent.append(i)
            return {"sent": sent}
        failures = [SimpleNamespace(signature=s) for s in args.get("failures") or []]
        asks = ask_policy.blocks_on_person(args["blocker"], args.get("question", ""), failures)
        who = ask_policy.resolve_assignee(args.get("asked_of", ""), args.get("member_ref", ""), args.get("participants") or [], args.get("asker", ""))
        first = ask_policy.ask_dedupe_key(args.get("turn_id", ""), args.get("conv_id", ""), args.get("gap", ""))
        later = ask_policy.ask_dedupe_key(args.get("turn_id", ""), args.get("conv_id", ""), args.get("later_gap", args.get("gap", "")))
        return {"ask": asks, "assignee": who.user_id, "asked_of": who.asked_of, "same_key": first == later}
    return run
