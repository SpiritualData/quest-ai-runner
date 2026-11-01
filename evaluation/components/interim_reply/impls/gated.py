"""CURRENT (``reply-in-steps:gated`` and ``ask-on-block:structural``): the real ``core/interim_policy.py`` and ``core/ask_policy.py``. The turn
loop here is the orchestrator's own: one ``InterimLedger`` per turn, ``decide`` before each emit, ``record`` after a send. Zero model calls."""
from types import SimpleNamespace

from quest_ai_runner.core import ask_policy, interim_policy


def build(meter):  # noqa: ARG001
    def run(kind, args):
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
