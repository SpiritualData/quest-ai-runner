"""Qualitative QAR evaluation runner: real in-app Quest AI chat, DB side-effect diffs, LLM judge.

    .venv/bin/python3 evaluation/qualitative/runner.py validate      # datasets only, no world, no network
    .venv/bin/python3 evaluation/qualitative/runner.py setup [--wait SECONDS] [--partial]
    .venv/bin/python3 evaluation/qualitative/runner.py run [--dataset explicit|implicit|multistep|all]
                                                           [--only ID,ID] [--workers N] [--no-judge]
    .venv/bin/python3 evaluation/qualitative/runner.py report
    .venv/bin/python3 evaluation/qualitative/runner.py reset
    .venv/bin/python3 evaluation/qualitative/runner.py teardown [--keep-quests]

Each case drives ONE FRESH conversation of the real chat (POST
/api/quest-ai/conversations/{id}/messages/stream, auto_run true, the surface a subscriber uses).
Explicit cases pin the conversation to the case's quest; implicit cases leave quest_ids empty so
the assistant must find the right quest itself. See README.md and datasets/schema.md.
"""
import argparse
import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import judge as J  # noqa: E402
import world as W  # noqa: E402
from devclient import (  # noqa: E402
    QUEST_BASE, WORK_DIR, api, create_conversation, delete_conversation, sse_send)

DATASET_DIR = HERE / "datasets"
RAW_DIR = WORK_DIR / "raw"
RESULTS_JSON = WORK_DIR / "results.json"
RESULTS_MD = HERE / "RESULTS.md"
DATASETS = ("explicit", "implicit", "multistep")
PASS_THRESHOLD = 0.7
RESET_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------------------------

PRECHECK_KEYS = frozenset({
    "reply_contains_all", "reply_contains_any", "reply_not_contains", "reply_regex",
    "reply_regex_forbidden", "code_contains_all", "code_contains_any", "code_not_contains",
    "tools_called", "tools_not_called", "pivot_values_in_reply"})
WRITE_ENTITIES = frozenset({"quest_field", "quest_note", "goal", "entry", "task"})
WRITE_OPS = frozenset({"contains", "equals", "number_close", "gte", "lte", "regex", "exists",
                       "not_contains"})


def validate_case(case, source):
    problems = []
    for key in ("id", "area", "rubric"):
        if not case.get(key):
            problems.append(f"missing {key}")
    if not (case.get("message") or case.get("messages")):
        problems.append("needs message or messages[]")
    if case.get("dataset") not in DATASETS:
        problems.append(f"dataset must be one of {DATASETS}")
    if case.get("dataset") == "explicit" and not case.get("quest_key"):
        problems.append("explicit cases need quest_key")
    if case.get("conversation_scope") not in (None, "quest", "none", "world"):
        problems.append(f"bad conversation_scope {case.get('conversation_scope')}")
    if case.get("expected_routing") not in (None, "inline", "deep", "delegated", "any"):
        problems.append(f"bad expected_routing {case.get('expected_routing')}")
    for key in ("must_use_pivots", "bonus_pivots"):
        for name in case.get(key) or []:
            if name not in W.PIVOTS:
                problems.append(f"unknown pivot {name} in {key}")
    overlap = set(case.get("must_use_pivots") or []) & set(case.get("bonus_pivots") or [])
    if overlap:
        problems.append(f"pivots in both must_use and bonus: {sorted(overlap)}")
    # A misspelled pre-check key used to be silently ignored, so the case looked strict and
    # asserted nothing at all.
    for key in (case.get("precheck") or {}):
        if key not in PRECHECK_KEYS:
            problems.append(f"unknown precheck key {key!r}")
    for spec in case.get("expect_writes") or []:
        if spec.get("entity") not in WRITE_ENTITIES:
            problems.append(f"bad expect_writes entity {spec.get('entity')}")
        if spec.get("op", "contains") not in WRITE_OPS:
            problems.append(f"bad expect_writes op {spec.get('op')}")
        if spec.get("entity") == "entry" and not (spec.get("match") or {}).get("collection_key"):
            problems.append("an entry expect_writes needs match.collection_key")
        if spec.get("entity") == "quest_note":
            problems.append("quest_note writes are impossible on this surface: the chat has no "
                            "add-note helper and `notes` is not one of the five raw collections "
                            "(see world.CHAT_CAPABILITIES). Assert on the reply instead.")
    if problems:
        raise ValueError(f"{source}: case {case.get('id')!r}: {'; '.join(problems)}")


def load_cases(dataset="all", only=None):
    cases = []
    for path in sorted(DATASET_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text())
        for case in (data["cases"] if isinstance(data, dict) else data):
            case.setdefault("dataset", path.stem)
            validate_case(case, path.name)
            cases.append(case)
    ids = [c["id"] for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate case ids: {sorted(dupes)}")
    if dataset != "all":
        cases = [c for c in cases if c["dataset"] == dataset]
    if only:
        cases = [c for c in cases if c["id"] in set(only)]
    return cases


def validate_datasets():
    """Load and validate every dataset without touching the world or the network.

    Authoring guard: `run` validates on load too, but only after a world exists and a backend is
    reachable, so a schema mistake used to surface minutes into a session (or on someone else's
    machine). This is the cheap check to run after editing a dataset file.
    """
    cases = load_cases("all")
    examples = load_example_cases()
    per_dataset = {ds: sum(1 for c in cases if c["dataset"] == ds) for ds in DATASETS}
    pivot_use = sum(1 for c in cases if c.get("must_use_pivots"))
    writes = sum(1 for c in cases if c.get("expect_writes"))
    print(f"datasets    : {', '.join(f'{ds}={n}' for ds, n in per_dataset.items())}")
    print(f"cases       : {len(cases)} valid, no duplicate ids ({len(examples)} example cases too)")
    print(f"             {pivot_use} with required pivots, {writes} with expect_writes, "
          f"{sum(1 for c in cases if c.get('forbid_writes'))} write-forbidden")
    unused = sorted(set(W.PIVOTS) - {p for c in cases
                                     for p in (c.get("must_use_pivots") or [])
                                     + (c.get("bonus_pivots") or [])})
    if unused:
        print(f"NOTE: pivots no case references: {unused}")
    print("VALIDATION OK")
    return cases


def load_example_cases():
    """The two smoke cases in datasets/_example.json (ignored by normal runs)."""
    data = json.loads((DATASET_DIR / "_example.json").read_text())
    cases = data["cases"] if isinstance(data, dict) else data
    for case in cases:
        validate_case(case, "_example.json")
    return cases


def mutates(case):
    """Cases that may write share the world and run serially. Default: anything not explicitly
    read-only (forbid_writes true and no expect_writes) is treated as mutating."""
    if "mutates" in case:
        return bool(case["mutates"])
    return bool(case.get("expect_writes")) or not case.get("forbid_writes")


# ---------------------------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------------------------

def conversation_quests(case, world):
    scope = case.get("conversation_scope") or (
        "none" if case["dataset"] == "implicit" else "quest")
    if scope == "none":
        return []
    if scope == "world":
        return list(world["quests"].values())
    if scope == "quest":
        return [world["quests"][case["quest_key"]]]
    raise ValueError(f"bad conversation_scope {scope}")


def ground_truth_keys(case):
    """Which quests' seeded data the judge is shown, the case's quest FIRST.

    A pinned (explicit) case only needs its own quest. A case the assistant had to choose a quest
    for needs them ALL, or the judge cannot tell a right choice from a wrong one, and cannot check
    the figures on the quest the case says is the wrong target (IMP-014 asks about both the launch
    total and the bathroom total, and only ever saw one of them).
    """
    scope = case.get("conversation_scope") or (
        "none" if case["dataset"] == "implicit" else "quest")
    key = case.get("quest_key")
    if scope == "quest" and key:
        return [key]
    keys = list(W.QUESTS)
    if key in keys:
        keys.remove(key)
        keys.insert(0, key)
    return keys


def run_case(case, world, use_judge=True, parallel=False):
    started = time.time()
    record = {"id": case["id"], "dataset": case["dataset"], "area": case["area"],
              "quest_key": case.get("quest_key"), "mutates": mutates(case),
              "messages": case.get("messages") or [case["message"]]}
    conv = None
    try:
        before = W.snapshot(world)
        conv = create_conversation(conversation_quests(case, world))
        record["conversation"] = conv
        turns = []
        for message in record["messages"]:
            events = sse_send(conv, message, auto_run=case.get("auto_run", True))
            turns.append({"message": message, "events": events})
        time.sleep(1.0)  # let the backend finish persisting writes before the second snapshot
        after = W.snapshot(world)
        changes = W.diff(before, after)
        queued = W.conversation_tasks(conv)
        for task in queued:
            tid = W.task_id_of(task)
            if not any(tid in c for c in changes):
                changes.append(f"TASK QUEUED via chat {tid}: {(task.get('text') or '')[:160]!r}")
            # A task the chat queued is only in ``after["tasks"]`` when it happens to carry the
            # tag or sit on a world quest, so every expect_writes on a task used to look for a row
            # the snapshot never held. Put it there before the pre-checks read it.
            after["tasks"].setdefault(tid, (task.get("text") or "")[:160])
            api("DELETE", f"/api/assistant-tasks/{tid}")  # never let the dev lane execute it
        evidence = J.build_evidence(turns, queued)
        pre = J.precheck(case, evidence, world, after, changes)
        record.update({"evidence": evidence, "changes": changes, "precheck": pre,
                       "side_effects_ambiguous": bool(parallel and changes)})
        if use_judge:
            truth = W.ground_truth(ground_truth_keys(case))
            if pre["hard_failures"] and not case.get("judge_always", False):
                record["judged"] = {"skipped": "hard pre-check failure"}
            else:
                record["judged"] = J.judge(case, evidence, changes, pre, truth, W.PIVOTS)
        record["score"] = J.final_score(pre, record.get("judged"))
        record["passed"] = J.case_passed(case, pre, record.get("judged"), PASS_THRESHOLD)
        verdict = (record.get("judged") or {}).get("verdict")
        if verdict:
            record["pivot_gate"] = J.pivot_gate(case, verdict)
        record["_before_after"] = (before, after)
    except Exception as e:  # noqa: BLE001
        record["error"] = f"{type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc()[-1500:]
        record["score"], record["passed"] = None, False
    finally:
        if conv:
            delete_conversation(conv)
    record["seconds"] = round(time.time() - started, 1)
    return record


def restore_world(record, world):
    """After a mutating case, put the world back; fall back to a full reset (no approvals needed)."""
    pair = record.pop("_before_after", None)
    if pair is None:
        return world
    before, after = pair
    if not W.diff(before, after):
        return world
    ok = False
    try:
        ok = W.revert(before, after, world)
    except Exception as e:  # noqa: BLE001
        print(f"      revert raised {e}")
    record["reverted"] = ok
    if not ok:
        print("      revert incomplete: resetting the world")
        with RESET_LOCK:
            W.reset()
        world = W.load()
        record["world_reset"] = True
    return world


def print_row(r):
    judged = (r.get("judged") or {}).get("verdict") or {}
    flag = "PASS" if r.get("passed") else "FAIL"
    score = "n/a" if r.get("score") is None else f"{r['score']:.2f}"
    print(f"[{r['id']:8}] {flag} score={score} route={'ok' if judged.get('routing_ok', True) else 'BAD'} "
          f"fx={'ok' if judged.get('side_effects_ok', True) else 'BAD'} "
          f"changes={len(r.get('changes') or [])} ({r['seconds']}s)")
    if r.get("error"):
        print(f"           ERROR {r['error']}")
    pre = r.get("precheck") or {}
    if pre.get("hard_failures"):
        print(f"           hard fail: {pre['hard_failures'][:4]}")
    if r.get("pivot_gate"):
        print(f"           context gate: {r['pivot_gate']}")
    if judged.get("summary"):
        print(f"           {judged['summary']}")
    elif (r.get("judged") or {}).get("error"):
        print(f"           JUDGE ERROR {r['judged']['error']}")


def save_result(record):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{record['id']}.json").write_text(json.dumps(record, indent=1, default=str))


def merge_results(records):
    existing = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else {}
    for r in records:
        existing[r["id"]] = r
    RESULTS_JSON.write_text(json.dumps(existing, indent=1, default=str))


def run_cases(cases, use_judge=True, workers=1):
    world = W.load()
    print(f"Quest backend : {QUEST_BASE} (in-app Quest AI chat, auto_run on)")
    print(f"World         : {len(world['quests'])} quests, {len(world['collections'])} collections"
          f"{' (PARTIAL smoke world)' if world.get('partial') else ''}")
    print(f"Cases         : {len(cases)} ({sum(1 for c in cases if not mutates(c))} parallel-safe, "
          f"{sum(1 for c in cases if mutates(c))} mutating, run serially)\n")
    read_only = [c for c in cases if not mutates(c)]
    mutating = [c for c in cases if mutates(c)]
    records = []
    if read_only:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for r in pool.map(lambda c: run_case(c, world, use_judge, parallel=workers > 1),
                              read_only):
                r.pop("_before_after", None)
                records.append(r)
                save_result(r)
                print_row(r)
    W.delete_new_cards(world.get("cards_baseline"))
    for case in mutating:
        r = run_case(case, world, use_judge)
        world = restore_world(r, world)
        W.delete_new_cards(world.get("cards_baseline"))
        records.append(r)
        save_result(r)
        print_row(r)
    merge_results(records)
    done = [r for r in records if r.get("score") is not None]
    print("\n==================== SUMMARY ====================")
    print(f"cases run   : {len(records)}   passed: {sum(bool(r['passed']) for r in records)}   "
          f"errors/unjudged: {len(records) - len(done)}")
    if done:
        print(f"mean score  : {sum(r['score'] for r in done) / len(done):.2f}")
    print(f"raw JSON    : {RAW_DIR}   merged: {RESULTS_JSON}")
    return records


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------

def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def fmt(value):
    return "n/a" if value is None else f"{value:.2f}"


def one_line(text, limit=160):
    return " ".join(str(text or "").split())[:limit].replace("|", "/")


def write_report():
    results = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else {}
    rows = sorted(results.values(), key=lambda r: (r["dataset"], r["id"]))
    lines = ["# Qualitative QAR evaluation results", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} against `{QUEST_BASE}` (dev). "
             "Surface: the real in-app Quest AI chat, auto_run on, one fresh conversation per case. "
             "Judge: `claude -p` sonnet over the transcript evidence plus a database side-effect "
             "diff, after deterministic pre-checks. Raw JSON per case: `/tmp/qualeval/raw/`.", ""]
    if not rows:
        lines.append("No results yet. Run `runner.py run`.")
        RESULTS_MD.write_text("\n".join(lines) + "\n")
        return
    lines += ["## By dataset", "", "| dataset | cases | passed | mean score | routing ok | side effects ok |",
              "|---|---|---|---|---|---|"]
    for ds in DATASETS:
        sub = [r for r in rows if r["dataset"] == ds]
        if not sub:
            continue
        verdicts = [((r.get("judged") or {}).get("verdict") or {}) for r in sub]
        judged = [v for v in verdicts if v]
        lines.append(f"| {ds} | {len(sub)} | {sum(bool(r['passed']) for r in sub)} | "
                     f"{fmt(mean(r.get('score') for r in sub))} | "
                     f"{sum(v['routing_ok'] for v in judged)}/{len(judged)} | "
                     f"{sum(v['side_effects_ok'] for v in judged)}/{len(judged)} |")
    lines += ["", "## By area", "", "| area | cases | passed | mean score |", "|---|---|---|---|"]
    for area in sorted({r["area"] for r in rows}):
        sub = [r for r in rows if r["area"] == area]
        lines.append(f"| {area} | {len(sub)} | {sum(bool(r['passed']) for r in sub)} | "
                     f"{fmt(mean(r.get('score') for r in sub))} |")
    classes = {}
    for r in rows:
        fc = (((r.get("judged") or {}).get("verdict") or {}).get("failure_class"))
        if fc and not r.get("passed"):
            classes.setdefault(fc, []).append(r["id"])
    if classes:
        lines += ["", "## Failure classes", ""]
        for fc, ids in sorted(classes.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- `{fc}`: {len(ids)} ({', '.join(ids)})")
    pivot_stats = {}
    for r in rows:
        for c in (((r.get("judged") or {}).get("verdict") or {}).get("context_used") or []):
            s = pivot_stats.setdefault(c["pivot"], [0, 0, 0])
            s[0] += 1
            s[1] += bool(c["used"])
            s[2] += bool(c["changed_answer"])
    if pivot_stats:
        lines += ["", "## Pivot use", "", "| pivot | cases | used | changed the answer |", "|---|---|---|---|"]
        for name, (n, used, changed) in sorted(pivot_stats.items()):
            lines.append(f"| {name} | {n} | {used} | {changed} |")
    lines += ["", "## Per case", "",
              "| id | dataset | area | pass | score | route | effects | secs | verdict |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        v = ((r.get("judged") or {}).get("verdict") or {})
        lines.append(
            f"| {r['id']} | {r['dataset']} | {r['area']} | {'yes' if r.get('passed') else 'NO'} | "
            f"{fmt(r.get('score'))} | {'ok' if v.get('routing_ok', True) else 'BAD'} | "
            f"{'ok' if v.get('side_effects_ok', True) else 'BAD'} | {r.get('seconds')} | "
            f"{one_line(r.get('error') or (r.get('precheck') or {}).get('hard_failures') or r.get('pivot_gate') or v.get('summary'))} |")
    RESULTS_MD.write_text("\n".join(lines) + "\n")
    print(f"report written to {RESULTS_MD}")


# ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command",
                        choices=["setup", "run", "teardown", "report", "reset", "validate"])
    parser.add_argument("--dataset", default="all", choices=["all", *DATASETS])
    parser.add_argument("--only", default=None, help="comma-separated case ids")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallelism for read-only cases (mutating cases always run serially)")
    parser.add_argument("--no-judge", action="store_true", help="deterministic pre-checks only")
    parser.add_argument("--examples", action="store_true",
                        help="run datasets/_example.json (pipeline smoke test) instead of datasets")
    parser.add_argument("--wait", type=int, default=0, help="setup: seconds to wait for approvals")
    parser.add_argument("--partial", action="store_true", help="setup: smoke world, no quests")
    parser.add_argument("--keep-quests", action="store_true")
    parser.add_argument("--decline-asks", action="store_true")
    args = parser.parse_args()
    global RESULTS_JSON, RESULTS_MD
    if args.examples:  # smoke runs never pollute the real results or RESULTS.md
        RESULTS_JSON, RESULTS_MD = WORK_DIR / "results_examples.json", WORK_DIR / "RESULTS_examples.md"
    if args.command == "validate":
        validate_datasets()
    elif args.command == "setup":
        W.setup_partial() if args.partial else W.setup(args.wait)
    elif args.command == "reset":
        W.reset()
    elif args.command == "teardown":
        sys.exit(0 if W.teardown(args.keep_quests, args.decline_asks) else 1)
    elif args.command == "report":
        write_report()
    else:
        only = args.only.split(",") if args.only else None
        cases = load_example_cases() if args.examples else load_cases(args.dataset, only)
        if args.examples and only:
            cases = [c for c in cases if c["id"] in only]
        if not cases:
            raise SystemExit("no cases matched")
        run_cases(cases, use_judge=not args.no_judge, workers=args.workers)
        write_report()


if __name__ == "__main__":
    main()
