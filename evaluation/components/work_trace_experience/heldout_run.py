"""Held-out tier for the Trail: capture the REAL turns of ``heldout_cases.json`` once (``live_turns.run_turn``, same store fixtures), then score
them for any arm with the esoi runner, recorded with the held-out file's own hash. Nothing in the Trail or the cases is edited between the first run
and the report.

    python3 heldout_run.py capture --out /tmp/held.json [--only TRACE-HELD-WEB,...]
    python3 heldout_run.py score --captured /tmp/held.json --impl trail [--record]
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))

SPECS = HERE / "heldout_cases.json"


def sha():
    return hashlib.sha256(SPECS.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:8]


def capture(args):
    import live_turns
    specs = json.loads(SPECS.read_text(encoding="utf-8"))
    if args.only:
        keep = set(args.only.split(","))
        specs = [s for s in specs if s["id"] in keep]
    key = os.environ.get("GOOGLE_AI_API_KEY")
    out = []
    for spec in specs:
        live_turns.TURNS[spec["id"]] = {k: spec[k] for k in ("kind", "surface", "audience", "message")}
        if spec.get("search") == "unavailable":
            os.environ.pop("GOOGLE_AI_API_KEY", None)   # no web backend: the run cannot search
        elif key:
            os.environ["GOOGLE_AI_API_KEY"] = key
        case = live_turns.run_turn(spec["id"])
        case.update(id=spec["id"], heldout=True, want=spec["want"])
        out.append(case)
        print(f"{spec['id']}: {len(case['events'])} events, {case['seconds']}s", flush=True)
        Path(args.out).write_text(json.dumps(out, indent=1))


def score(args):
    import common
    from esoi import harness
    cases = json.loads(Path(args.captured).read_text())
    harness.load_cases = lambda component, config: cases
    harness.cases_digest = lambda component, eval_dir: sha()
    harness.record_slices = lambda *a, **k: []   # the per-case log compacts rows pinned to cases.json's hash; held-out rows go to results.csv only
    argv = ["--impl", args.impl] + (["--record"] if args.record else [])
    harness.run_component("work_trace_experience", __import__("work_trace_experience.score", fromlist=["x"]).score_case, argv, common.CONFIG)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument("--only")
    s = sub.add_parser("score")
    s.add_argument("--captured", required=True)
    s.add_argument("--impl", required=True)
    s.add_argument("--record", action="store_true")
    args = ap.parse_args()
    capture(args) if args.cmd == "capture" else score(args)


if __name__ == "__main__":
    main()
