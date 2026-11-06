"""Secondary routing check on REAL requests: run QAR's planning call on request texts from the private context datasets
(``QAR_PRIVATE_CONTEXT_DATASET``, folders separated by ``os.pathsep``) and check the route against each case's own task shape.

Nothing from the data is written anywhere: the output is counts per task shape and per route, plus case ids of failures. The
planner sees the request text with no quest view and no tools beyond the standard catalogue; no deep run, no Quest, no writes.
PAID in subscription calls (``claude -p``). Stage it: ``--sample N`` (a stratified, repeatable draw by task shape), ``--max-calls``.

Acceptable routes by task shape (a judgement, written here so it can be argued with):
  edit_files                 the work is handed to a deep or queued run, or the planner asks or confirms; a plain answer or a
                             direct tool call would claim or attempt code work it cannot do inline
  real_world_action          confirm, clarify, delegated or an honest answer; a direct tool call is a write to the world
  draft_message              answer, clarify or delegated; a direct tool call would send or save something unasked
  plan_schedule, data_entry  anything (a tool call is a legitimate way to record or schedule)
  answer_question, analysis_strategy   anything except a write tool call
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation" / "components"))

SHAPE_QUOTA = {"edit_files": 10, "real_world_action": 6, "answer_question": 3, "plan_schedule": 3, "draft_message": 3,
               "analysis_strategy": 3, "data_entry": 2}  # a 30-case draw; other sizes scale it


def load():
    cases = {}
    for folder in os.environ.get("QAR_PRIVATE_CONTEXT_DATASET", "").split(os.pathsep):
        path = Path(folder) / "cases.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            row = json.loads(line)
            cases.setdefault(row["case_id"], row)
    return list(cases.values())


def draw(cases, size):
    by_shape = defaultdict(list)
    for case in sorted(cases, key=lambda c: hashlib.sha256(c["case_id"].encode()).hexdigest()):
        by_shape[case["task_shape"]].append(case)
    picked = []
    for shape, quota in SHAPE_QUOTA.items():
        picked += by_shape[shape][:max(1, round(quota * size / 30))]
    return picked[:size] if size < len(SHAPE_QUOTA) else picked


def facts(plan):
    queued = bool(getattr(plan, "deferred_deep", None))
    target = getattr(plan, "deep_target", None)
    delegated = (plan.action == "deep" and target != "records") or (plan.action == "answer" and queued)
    route = "delegated" if delegated else plan.action
    return route, plan.action == "tool" or (plan.action == "deep" and target == "records")


def verdict(shape, route, writes):
    if shape == "edit_files":
        return route in ("delegated", "clarify", "confirm") or (route == "deep")
    if shape in ("real_world_action", "draft_message", "answer_question", "analysis_strategy"):
        return not (route == "tool")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=3)
    parser.add_argument("--max-calls", type=int, required=True)
    args = parser.parse_args(argv)
    import offline_orchestrator

    from esoi.optimization.measurement_harness.call_meter import CallMeter
    meter = CallMeter(max_calls=args.max_calls)
    meter.model = os.environ.get("QAR_EVAL_MODEL", "haiku")
    orch = offline_orchestrator.build_offline_orchestrator(meter, planner_tier=meter.model)
    cases = draw(load(), args.sample)
    print(f"route_private_requests: {len(cases)} cases, budget {args.max_calls}, ids {[c['case_id'] for c in cases]}")
    table, failures = defaultdict(Counter), []
    for case in cases:
        route, writes = facts(orch._plan(case["input_text"], "", "No quest is pinned to this conversation.", []))
        table[case["task_shape"]][route] += 1
        if not verdict(case["task_shape"], route, writes):
            failures.append((case["case_id"], case["task_shape"], route))
    ok = len(cases) - len(failures)
    print(f"route_private_requests: {ok}/{len(cases)} acceptable, {meter.calls} model calls, {meter.tokens_in} in / {meter.tokens_out} out tokens")
    for shape, counts in sorted(table.items()):
        print(f"  {shape}: {dict(counts)}")
    for item in failures:
        print("  FAIL", *item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
