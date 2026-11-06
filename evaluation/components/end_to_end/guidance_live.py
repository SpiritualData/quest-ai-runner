"""Live driver for ``cases_guidance.json``: the real orchestrator over the end-to-end world, scored by the world and the reply
(``score.judge``), on a model chosen by ``--model`` (default ``claude-haiku-5-5`` through the Claude Code subscription, which is what Quest AI
runs on; a ``gemini-*`` model is the failover check only; the Anthropic API is not used). Stay on the ladder of esoi/metric-run-policy.md: pass ``--only`` ids (3 cases, then 10) and ``--max-calls``.

    .venv/bin/python3 evaluation/components/end_to_end/guidance_live.py --only E2E-GUIDE-01,E2E-GUIDE-04 --max-calls 40

Prints one line per case with ESTIMATED tokens (4 characters each) and cost (list price from ``prices`` in esoi.toml); ``--json`` writes the rows."""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402
import offline_orchestrator  # noqa: E402
from end_to_end import drive, score  # noqa: E402

GUIDANCE = json.loads((HERE / "cases_guidance.json").read_text(encoding="utf-8"))
# Existing end_to_end cases (read only, never edited here) that a regression check may draw on by id.
CASES = GUIDANCE + json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def use_model(model):
    """Pin every QAR tier to ``model`` for this process (the offline builder scrubs the tier variables, so stop it doing so)."""
    backend = "gemini" if model.startswith("gemini") else "claude_cli"   # Claude models run on the Claude Code subscription: the API is not used
    os.environ.update(QAR_MODEL_BACKEND=backend, QAR_MODEL_FAST=model, QAR_MODEL_BALANCED=model, QAR_MODEL_QUALITY=model, QAR_MODEL_BEST=model)
    common.SCRUB = tuple(v for v in common.SCRUB if not v.startswith("QAR_MODEL_"))


def estimate_tokens(orch):
    """The Gemini provider keeps no token counters, so count characters sent and received on every leaf call (4 per token): an
    estimate, which is why the report says so. Returns a dict the wrapped providers add to."""
    import functools
    seen = {"in": 0, "out": 0, "calls": 0}
    leaves = getattr(orch.provider, "providers", None)
    for leaf in (leaves.values() if isinstance(leaves, dict) else leaves or []):
        for name in ("plan", "answer"):
            orig = getattr(leaf, name, None)
            if orig is None:
                continue

            def make(fn):
                @functools.wraps(fn)
                def counted(*a, **k):
                    out = fn(*a, **k)
                    seen["calls"] += 1
                    seen["in"] += (len(str(a[0])) if a else 0) + len(str(k.get("system") or "")) + len(str(k.get("tool_schema") or ""))
                    seen["out"] += len(str(out))
                    return out
                return counted
            setattr(leaf, name, make(orig))
    return seen


def usage_of(seen):
    return seen["in"] // 4, seen["out"] // 4


def run_case(case, model, max_calls):
    meter = common.CallMeter(max_calls=max_calls)
    meter.model = model
    world = drive.new_world(case)
    orch = offline_orchestrator.build_offline_orchestrator(meter, registry=world.registry, planner_tier="fast")
    orch.web = world.web
    if world.web is not None:
        # The web-read gate acts on the reach judge's verdict, which a deployment opts into with a reach summary.
        orch.cfg.planner_reach_judge = True
        orch.cfg.read_reach_summary = ("Reads reach the person's quests, goals and notes. Anything about the world right now "
                                       "(prices, versions, news) is reachable only through a web search.")
    hosts = tuple((case.get("world") or {}).get("trusted_link_hosts") or ())
    if hosts:   # the offline world has no network: a host the case names is one whose link the guard may keep without fetching it
        from hubofhubs.api import LinkPolicy
        orch.cfg.link_policy = LinkPolicy(trusted_hosts=hosts, check_external=False)
    seen = estimate_tokens(orch)
    got = drive.run_with(case, world, orch)
    verdict = score.judge(got, case)
    return verdict, got, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    use_model(args.model)
    ids = [x for x in args.only.split(",") if x] or [c["id"] for c in GUIDANCE]
    prices = common.CONFIG.prices.get(args.model, {"input": 0.0, "output": 0.0}) if hasattr(common.CONFIG, "prices") else {"input": 0.0, "output": 0.0}
    rows, total = [], 0.0
    for case in CASES:
        if case["id"] not in ids:
            continue
        verdict, got, seen = run_case(case, args.model, args.max_calls)
        tin, tout = usage_of(seen)
        cost = (tin * prices["input"] + tout * prices["output"]) / 1e6
        total += cost
        rows.append(dict(id=case["id"], ok=verdict["ok"], note=verdict["note"], tokens_in=tin, tokens_out=tout, cost=round(cost, 5), calls=seen["calls"], reply=got["reply"]))
        print(f"{case['id']} {'PASS' if verdict['ok'] else 'FAIL'} in={tin} out={tout} ${cost:.4f} calls={seen['calls']} {verdict['note'][:160]}", flush=True)
    print(f"passed {sum(r['ok'] for r in rows)}/{len(rows)} total ${total:.4f}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
