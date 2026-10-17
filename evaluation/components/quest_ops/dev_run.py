"""Run the dev-Quest arm of ``quest_ops`` on ``dev_cases.json`` (58 cases: the 50 chat quest-operation cases
plus the 8 recipe cases). One command, skipped (exit 0) when the dev Quest is not configured:

    export QAR_EVAL_DEV_ENV_FILE=<the dev lane's env file> QAR_EVAL_DEV_HOST=<its host>
    PY=.venv/bin/python3
    $PY evaluation/components/quest_ops/dev_run.py --sample 12 --seed 1      # a stratified draw
    $PY evaluation/components/quest_ops/dev_run.py --only R1,H1,G3,X1
    $PY evaluation/components/quest_ops/dev_run.py --stratum write --auto-run off   # approval-card arm
    $PY evaluation/components/quest_ops/dev_run.py --selftest                # verifier controls, no chat
    $PY evaluation/components/quest_ops/dev_run.py --recipes-subset 10 --recipes on   # recipe cases

Each case scores two things: ROUTING (``expect_route`` ``inline`` passes unless the turn was delegated;
``deep`` passes only when it was) and EXECUTION (the case's ``verify`` data against what the dev Quest really
shows). With ``--auto-run off`` (the app's real default) only write cases run: the write must be held before
"Yes, go ahead." and applied after it. One fixture quest per run, torn down and verified gone at the end.
A full run costs real model tokens on dev: draw a ``--sample`` (default none; ``--sample`` is stratified by
``stratum``) and keep passes to one, since single passes are noisy. ``--recipes on`` switches the backend's recipe
store via ``QAR_EVAL_RECIPES_DIR`` (the ENABLED flag file) and restores it afterwards."""
import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dev_arm  # noqa: E402
import dev_verify  # noqa: E402
import dev_world  # noqa: E402

CASES = HERE / "dev_cases.json"
SUBSETS = HERE / "dev_recipe_subsets.json"


def load_cases():
    return json.loads(CASES.read_text())


def draw_sample(cases, count, seed):
    """``count`` cases spread over the strata in proportion (at least one each while count allows)."""
    rng = random.Random(seed)
    strata = {}
    for case in cases:
        strata.setdefault(case["stratum"], []).append(case)
    chosen = []
    for rows in strata.values():
        take = max(1, round(count * len(rows) / len(cases)))
        chosen += rng.sample(rows, min(take, len(rows)))
    rng.shuffle(chosen)
    return chosen[:count]


def routing_ok(case, res):
    return (res.route == "delegated") if case["expect_route"] == "deep" else (res.route != "delegated")


def score_case(fn, case, world):
    """Open the case's conversation, replay its prelude and before-hook, send, then check route and state.
    Under ``--auto-run off`` the write must not have landed before approval and must have after it."""
    dc = dev_world.client()
    world.conv = dc.create_conversation([world.quest_id])
    world.fx["conversation_ids"].append(world.conv)
    for earlier in case.get("prelude") or []:
        dev_arm.send(world, world.conv, earlier)
    if case.get("before"):
        dev_verify.run_before(case["before"], world.fx)
    started = time.time()
    out = fn(case["message"], world)
    res = world.last
    try:
        ok, note = dev_verify.verify(case["verify"], res, world.fx)
    except Exception as e:  # noqa: BLE001
        ok, note = False, f"verifier raised {type(e).__name__}: {e}"
    row = dict(routing_ok=routing_ok(case, res), reply=out["reply"][:600], seconds=round(time.time() - started, 1),
               **{k: v for k, v in out.items() if k != "reply"})
    if not world.auto_run:
        held = not ok
        world.fx["decision_ids"] += [d.get("decision_id") or d.get("id")
                                     for d in dev_world.decisions_for_quest(world.quest_id) if d.get("status") == "open"]
        yes = dev_arm.send(world, world.conv, "Yes, go ahead.")
        landed, note2 = dev_verify.verify(case["verify"], yes, world.fx)
        ok, note = held and landed, f"{note} | after approval: {note2}"
        row.update(held_before_yes=held, landed_after_yes=landed)
    ok = bool(ok) and row["routing_ok"]
    row.update(ok=ok, got=res.route, note=note)
    return row


def selftest(cases, fx):
    """Verifier controls with no chat: each write case's check must be False on the untouched fixture and
    True after its own ``control`` steps are applied straight through REST."""
    class Blank:
        text, delegated, route = "", False, "answer"
    bad = []
    for case in [c for c in cases if c.get("control")]:
        if case.get("before"):
            dev_verify.run_before(case["before"], fx)
        neg, _ = dev_verify.verify(case["verify"], Blank(), fx)
        dev_world.apply_steps(fx, case["control"])
        pos, note = dev_verify.verify(case["verify"], Blank(), fx)
        print(f"[{case['id']:4}] negative={'PASS' if not neg else 'FAIL'} positive={'PASS' if pos else 'FAIL'} {note[:80]}")
        if neg or not pos:
            bad.append(case["id"])
    print("all verifiers passed both controls" if not bad else f"VERIFIER BUGS: {bad}")
    return not bad


def set_recipes(on):
    root = os.environ.get("QAR_EVAL_RECIPES_DIR")
    if not root:
        raise dev_world.DevUnavailable("set QAR_EVAL_RECIPES_DIR (the dev backend's recipes directory)")
    flag = Path(root).expanduser() / "ENABLED"
    flag.parent.mkdir(parents=True, exist_ok=True)
    if on:
        flag.write_text("on\n")
    elif flag.exists():
        flag.unlink()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--stratum", help="read, list, write, inform, contrast or recipe")
    ap.add_argument("--sample", type=int, help="draw this many cases, spread over the strata")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--auto-run", choices=["on", "off"], default="on")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--recipes-subset", choices=["1", "10", "30"], help="run a recipe teach-then-score subset")
    ap.add_argument("--recipes", choices=["on", "off"], default="off")
    ap.add_argument("--out", default="/tmp/qar_components/quest_ops_dev.json")
    args = ap.parse_args(argv)
    if not dev_world.configured():
        print("quest_ops dev arm: SKIPPED (QAR_EVAL_DEV_ENV_FILE is not set; the offline arms need no dev Quest)")
        return 0
    cases = load_cases()
    by_id = {c["id"]: c for c in cases}
    teach = []
    if args.recipes_subset:
        spec = json.loads(SUBSETS.read_text())[args.recipes_subset]
        teach, chosen = [by_id[i] for i in spec["teach"]], [by_id[i] for i in spec["score"]]
    elif args.only:
        chosen = [by_id[i.strip()] for i in args.only.split(",")]
    else:
        chosen = [c for c in cases if not args.stratum or c["stratum"] == args.stratum]
        if args.sample:
            chosen = draw_sample(chosen, args.sample, args.seed)
    if args.auto_run == "off":
        chosen = [c for c in chosen if c["stratum"] == "write"]
    fx = dev_world.setup()
    try:
        if args.selftest:
            return 0 if selftest(cases, fx) else 1
        if args.recipes_subset:
            set_recipes(args.recipes == "on")
        world = dev_arm.DevWorld(fx, auto_run=args.auto_run == "on")
        fn = dev_arm.build(None)
        for case in teach:  # taught first, not scored
            score_case(fn, case, world)
        rows = []
        for case in chosen:
            row = dict(id=case["id"], stratum=case["stratum"], area=case["area"], **score_case(fn, case, world))
            rows.append(row)
            print(f"[{row['id']:4}] {'PASS' if row['ok'] else 'FAIL'} route={row['got']:12} "
                  f"routing={'ok' if row['routing_ok'] else 'BAD'} {row['seconds']}s | {row['note'][:100]}")
        passed = sum(r["ok"] for r in rows)
        print(f"\nquest_ops / dev_chat: {passed}/{len(rows)} passed; routing ok "
              f"{sum(r['routing_ok'] for r in rows)}/{len(rows)}")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(dict(passed=passed, cases=len(rows), rows=rows), indent=1))
        print(f"results: {args.out}")
        return 0
    finally:
        if args.recipes_subset:
            set_recipes(False)
        print("cleanup:", "VERIFIED" if dev_world.teardown(fx) else "INCOMPLETE")


if __name__ == "__main__":
    sys.exit(main())
