"""CURRENT (``reply-in-steps:gated`` and ``ask-on-block:structural``): the real ``core/interim_policy.py`` and ``core/ask_policy.py``. The turn
loop here is the orchestrator's own: one ``InterimLedger`` per turn, ``decide`` before each emit, ``record`` after a send. Zero model calls offline. Its REAL cases (kind ``real``, run only with QAR_EVAL_LIVE_TURNS=1) run the real
orchestrator with ``interim_replies`` on."""
import json
from types import SimpleNamespace

from quest_ai_runner.the_consciousness.judgment_calls import ask_policy
from quest_ai_runner.the_intelligence.mind.response_writer import interim_policy


def interim_text(args):
    """The real ``maybe_send_interim`` with a stub orchestrator and sink: what an interim would show for one attempt's record."""
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import maybe_send_interim
    sent = []
    emit = SimpleNamespace(emit=sent.append)
    orch = SimpleNamespace(cfg=SimpleNamespace(interim_replies=True, max_interims_per_turn=4, min_seconds_between_interims=0.0))
    res = SimpleNamespace(output=args["output"], observations=list(args["receipts"]), observations_reported=bool(args["receipts"]),
                          coverage=args["coverage"], decision_id=None, claim_corrected=False)
    verdict = {"met": False, "reason": args["reason"], "next_action": args["next_action"], "blocker": "more_work",
               "progress_gap": args["progress_gap"], "progress_next": args["progress_next"], "claims_unexecuted": args["flagged"]}
    maybe_send_interim(orch, emit, {}, res, verdict, blocker="more_work", reason=args["reason"], attempt=1, loop_runs_again=True, now=100.0)
    shown = " ".join(str(e.text) + " " + json.dumps(e.data, default=str) for e in sent)
    return {"sent": bool(sent), "leaks": args["claim"] in shown, "has_receipt": any(r in shown for r in args["receipts"])}


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
            return interim_text(args)
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
