"""Score the quick-acknowledgement arms on REPLAYED turns (the overlay replay of the production chat), by the metric ``metric-narrate-a-turn``.

Arms, per case record (the replay's ``@@CASE@@`` record, see quest-backend ``scripts/checks/quest_chat_replay.py``):

  model_ack     the line of the separate fast-tier call (``model_ack_text``), at ``model_ack_seconds``; one extra model call
  mind_thought  the first planner call's own conversational line (``planner_first_thought.rationale``), at the moment that call
                returned (``planner_first_thought.seconds``); no extra call, but it cannot be shown before the planner returns
  hybrid        model_ack first; the mind's thought replaces it when it arrives (outcome and time are the model ack's, cost one call)

Outcome per case: the line passes ALL THREE items, each decided by a majority of three fast-tier views that must quote the words that
decide it (the hub's ``MajorityVote``; code checks each quote is in the evidence shown, or the view does not count): (1) the line is
specific to this request, (2) it claims nothing false or not yet known at that moment, (3) it matches what the turn then did.
Time: seconds from the person's message, a priced cost (lower is better); the 3 s target is reported, not a gate (Joshua, 2026-11-08).
Cost: extra model calls per turn.
Selection (fixed in the metric before any scoring): the higher share passing all three items; under one case in six apart is a tie and
goes to the lower median seconds, then the lower cost.

A line that is missing (no line, a dropped ack) fails the case. The weights are not tuned: nothing here is weighted, the share is a count.
"""
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

TARGET_SECONDS = 3.0      # reported per arm; not a gate
VIEWS = 3
ITEMS = [
    ("specific", "Is the line specific to this request: does it name something from the person's message (a topic, object or action they asked about) rather than being a generic 'looking into it' line?"),
    ("nothing_unknown", "Does the line claim NOTHING false or not yet known: no finding, figure, date, count or state of the person's account that the message and conversation do not contain, and nothing said to be already done or found?"),
    ("matches_turn", "Does the line match what the turn then did: is what the line says the AI will do or look at what the turn actually read, did or answered, judged from the reads and the reply shown?"),
]
OPTIONS = [{"id": "pass", "text": "the line meets this item"}, {"id": "fail", "text": "the line does not meet this item"}]
INSTRUCTION = ("{item}\nJudge only this item, from the evidence: the person's message, the line the AI said first, what the turn read and the reply it sent. "
               "Pick pass only if the evidence shows it, and quote the words that show it. Pick fail when it is not met or the evidence does not show it, "
               "and quote the words that show that when there are any.")


def lines_of(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The line each arm would show for this case and when: ``{arm: {"line", "seconds", "calls"}}``."""
    ack = (record.get("model_ack_text") or "").strip()
    thought = record.get("planner_first_thought") or {}
    mind = (thought.get("rationale") or "").strip()
    has_call = 1 if record.get("model_ack_calls") else 0
    return {
        "model_ack": {"line": ack, "seconds": record.get("model_ack_seconds"), "calls": 1},
        "mind_thought": {"line": mind, "seconds": thought.get("seconds"), "calls": 0},
        "hybrid": {"line": ack or mind, "seconds": record.get("model_ack_seconds") if ack else thought.get("seconds"), "calls": 1,
                   "then": mind, "then_seconds": thought.get("seconds"), "called": has_call},
    }


def evidence_text(record: Dict[str, Any], line: str) -> str:
    reads = "\n".join(f"- {str(r.get('source') or r)[:160]}" for r in (record.get("reads") or record.get("replay_reads") or [])[:20] if not str(r.get("source") or "").startswith("context view")) or "(none)"
    return ("The person's message:\n" + str(record.get("input") or ((record.get("judge_input") or {}).get("case") or {}).get("input") or "")[:2000]
            + "\n\nThe line the AI said first:\n" + line
            + "\n\nWhat the turn read:\n" + reads
            + "\n\nThe reply exactly as sent:\n" + str(record.get("reply") or record.get("replay_reply") or "")[:4000])


def judge_line(record: Dict[str, Any], line: str, ask: Callable[[str], str]) -> Dict[str, Any]:
    """Pass/fail for each item by a majority of three quote-checked views; ``ok`` when all three pass. No line, no pass."""
    if not line:
        return {"ok": False, "items": {}, "note": "no line"}
    from hubofhubs.api import CheckBinding, MajorityVote, View
    shown = evidence_text(record, line)

    def decide(item):
        name, question = item
        views = [View(f"view{n + 1}", INSTRUCTION.format(item=question), shown) for n in range(VIEWS)]
        binding = CheckBinding(kind="pick", options=OPTIONS, views=views, max_calls=VIEWS + 1, parallel=VIEWS, draft_basis="")
        got = MajorityVote(plumbing=None).check("", binding, ask)      # no draft pick: only the three views vote
        return name, {"pick": got.get("pick") or "fail", "outcome": got.get("outcome"), "agreement": got.get("agreement"), "calls": got.get("calls")}
    with ThreadPoolExecutor(3) as pool:
        items = dict(pool.map(decide, ITEMS))
    return {"ok": all(v["pick"] == "pass" for v in items.values()), "items": items}


def score(records: List[Dict[str, Any]], ask: Callable[[str], str], arms=("model_ack", "mind_thought", "hybrid")) -> Dict[str, Any]:
    """Per arm: share passing all three items, median seconds and whether it is inside the gate, extra model calls per turn, the judge's own calls."""
    out: Dict[str, Any] = {"cases": len(records), "target_seconds": TARGET_SECONDS, "arms": {}}
    judged: Dict[tuple, Dict[str, Any]] = {}
    for arm in arms:
        rows = []
        for rec in records:
            got = lines_of(rec)[arm]
            key = (rec.get("id") or rec.get("case_id"), got["line"])
            if key not in judged:        # the hybrid's first line is the model ack's: one judgment serves both
                judged[key] = judge_line(rec, got["line"], ask)
            verdict = judged[key]
            rows.append({"id": rec.get("id") or rec.get("case_id"), "line": got["line"], "seconds": got["seconds"], "ok": verdict["ok"], "items": verdict.get("items"), "calls": got["calls"]})
        times = [r["seconds"] for r in rows if isinstance(r["seconds"], (int, float))]
        median = round(statistics.median(times), 2) if times else None
        out["arms"][arm] = {"passing": sum(1 for r in rows if r["ok"]), "of": len(rows), "median_seconds": median,
                            "target_met": median is not None and median <= TARGET_SECONDS, "extra_calls_per_turn": max(r["calls"] for r in rows) if rows else 0, "rows": rows}
    out["selected"] = select(out["arms"], len(records))
    return out


def select(arms: Dict[str, Any], n: int) -> str:
    """The metric's fixed rule: the higher share passing all three items; closer than one case in six is a tie, then the lower median seconds, then the lower cost."""
    best = max(v["passing"] for v in arms.values())
    close = [a for a, v in arms.items() if (best - v["passing"]) < n / 6]
    return min(close, key=lambda a: (arms[a]["median_seconds"] if arms[a]["median_seconds"] is not None else 1e9, arms[a]["extra_calls_per_turn"], -arms[a]["passing"]))


if __name__ == "__main__":      # python ack_arms.py CASES.json  (a JSON list of replay case records)
    import sys
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider
    provider = ClaudeCliProvider(claude_path="claude", timeout_seconds=180)
    model = ModelRegistry(provider).resolve_tier("fast")
    records = json.load(open(sys.argv[1]))
    print(json.dumps(score(records, lambda prompt: provider.answer([{"role": "user", "content": prompt}], model=model)), indent=1, ensure_ascii=False))
