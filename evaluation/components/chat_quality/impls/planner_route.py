"""PLANNER-ONLY ROUTE CHECK (live model, no Quest, no world, no writes): the cheapest honest read of the chat dataset's
routing labels. For every user turn of a case it runs QAR's own planning call (``Orchestrator._plan``, the same arm as
``classification/qar_planner``) on the case's message, with the pinned quest's title and outcome as the quest view
(explicit cases), or the five world quests listed (implicit and the rest), and compares the decision with the case.

It scores only what a planner decision can show, and says so in each row:
  routing      ``expected_routing`` inline: the plan must not hand the work to a background or deep task (a deep action that
               targets the person's own records is a quick in-chat change and counts as inline); deep or delegated: the plan
               must be a deep action (not on records) or an answer that queues deferred deep work; any: always right.
  write safety ``forbid_writes``: the plan must not be a tool call or a deep action (either would write).
  steps        a multistep case whose steps name an ``expect_tool`` passes only when the plan, if it is a tool plan, calls
               a tool of that name (a plan that is not a tool plan is judged on routing alone).
It does NOT see context cards, pivot facts, the real tool results or the reply, so grounding, pivots, ``expect_writes`` and the
rubric stay unmeasured here: those need ``dev_quest_chat``. PAID in subscription calls (``claude -p``), no API dollars;
stay on the staged ladder and always pass ``--max-calls``."""
PAID = True
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import offline_orchestrator  # noqa: E402
import world_spec  # noqa: E402

WRITE_ACTIONS = ("tool", "deep")


def quest_view(case):
    quests = world_spec.QUESTS
    key = case.get("quest_key")
    if case.get("conversation_scope", "quest" if case.get("dataset") != "implicit" else "none") == "quest" and key in quests:
        return f"Quest: {key}\nOutcome: {quests[key]['outcome']}"
    return "Quests of this person:\n" + "\n".join(f"- {k}: {q['outcome']}" for k, q in quests.items())


def turns(case):
    return case.get("messages") or [case["message"]]


def decision_facts(plan):
    queued = bool(getattr(plan, "deferred_deep", None))
    target = getattr(plan, "deep_target", None)
    action = plan.action
    delegated = (action == "deep" and target != "records") or (action == "answer" and queued)
    writes = action == "tool" or (action == "deep" and target == "records") or delegated
    return {"action": action, "target": target, "queued": queued, "delegated": delegated, "writes": writes,
            "tools": [c.get("name") for c in (plan.tool_calls or [])]}


def judge_turn(case, facts):
    problems = []
    expected = case.get("expected_routing", "any")
    if expected == "inline" and facts["delegated"]:
        problems.append("routing: expected inline, plan delegated or queued deep work")
    if expected in ("deep", "delegated") and not facts["delegated"]:
        problems.append(f"routing: expected deep or delegated, plan was {facts['action']}")
    if case.get("forbid_writes") and facts["writes"]:
        problems.append(f"write safety: forbid_writes but plan was {facts['action']}"
                        f"{'+queued' if facts['queued'] else ''}")
    want = [s["expect_tool"] for s in case.get("steps", []) if s.get("expect_tool")]
    if want and facts["action"] == "tool" and not any(t in facts["tools"] for t in want):
        problems.append(f"steps: expected a call to one of {want}, got {facts['tools']}")
    return problems


def build(meter):
    orch = offline_orchestrator.build_offline_orchestrator(meter, planner_tier=os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def converse(case):
        view = quest_view(case)
        said, problems, seen = [], [], []
        for index, message in enumerate(turns(case)):
            transcript = "\n".join(f"User: {m}" for m in said)
            facts = decision_facts(orch._plan(message, transcript, view, []))
            seen.append(facts["action"] + ("+queued" if facts["queued"] else ""))
            problems += [f"turn {index + 1} {p}" for p in judge_turn(case, facts)]
            said.append(message)
        note = "; ".join(problems) or f"plan {','.join(seen)} fits {case.get('expected_routing', 'any')}"
        return {"passed": not problems, "score": 0.0 if problems else 1.0, "precheck": {"hard_failures": problems},
                "judged": {"summary": note}, "error": None, "seconds": 0}
    return converse
