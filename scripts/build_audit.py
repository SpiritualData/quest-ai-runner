#!/usr/bin/env python3
"""Build docs/structure/audit.csv: every component against principles P1 to P13 of the evolvable-structure skill.

Run from anywhere:  python scripts/build_audit.py
Objective columns are recomputed from components.csv, solutions.csv, waivers.csv and the files on disk;
`finding`, `action` and `tracking_*` are hand-edited and preserved. One row per component, iterated in place.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "docs" / "structure"
OUT = S / "audit.csv"
HAND = ["finding", "action", "tracking_status", "tracking_owner", "tracking_updated", "tracking_notes"]
COLS = ["component_id", "files", "max_module_lines", "solutions", "unmeasured_solutions", "waivers", "status",
        "claimed_principles", "unclaimed_core_principles", "verdict"] + HAND
CORE = ["P2", "P3", "P4", "P9", "P13"]  # the ones a code component can fail objectively
SKIP_DIRS = {".venv", ".git", "__pycache__", "scratch", "logs", "quest_ai_runner.egg-info"}


def rows(name):
    return list(csv.DictReader(open(S / name, newline="")))


def files(globs):
    out = set()
    for g in globs.split(";"):
        rx = re.compile(re.escape(g).replace(r"\*\*", "\x00").replace(r"\*", "[^/]*").replace("\x00", ".*"))
        for root, dirs, fs in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in fs:
                p = os.path.relpath(os.path.join(root, f), ROOT)
                if rx.fullmatch(p):
                    out.add(p)
    return out


def lines(p):
    try:
        return sum(1 for _ in open(ROOT / p, errors="ignore"))
    except OSError:
        return 0


def main():
    sols = {s["solution_id"]: s for s in rows("solutions.csv")}
    waivers = rows("waivers.csv")
    old = {r["component_id"]: r for r in csv.DictReader(open(OUT, newline=""))} if OUT.exists() else {}
    out = []
    for c in rows("components.csv"):
        fs = files(c["path"])
        code = [f for f in fs if f.endswith(".py")]
        mx = max((lines(f) for f in code), default=0)
        ids = [x for x in c["solutions"].split(";") if x]
        unmeasured = [i for i in ids if sols.get(i, {}).get("metric_command", "none") == "none"]
        wv = [w for w in waivers if any(w["path"].startswith(f) for f in fs)]
        claimed = ["P" + x for x in c["rules"].split(";") if x]
        unclaimed = [p for p in CORE if p not in claimed]
        if c["status"] != "done" or c["gap"]:
            verdict = "gap"
        elif unmeasured or wv:
            verdict = "partial"
        else:
            verdict = "meets"
        row = dict(component_id=c["component_id"], files=len(fs), max_module_lines=mx, solutions=";".join(ids),
                   unmeasured_solutions=";".join(unmeasured), waivers=len(wv), status=c["status"],
                   claimed_principles=";".join(claimed), unclaimed_core_principles=";".join(unclaimed), verdict=verdict)
        for h in HAND:
            row[h] = old.get(c["component_id"], {}).get(h, "")
        out.append(row)
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, COLS)
        w.writeheader()
        w.writerows(out)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(out)} components")


if __name__ == "__main__":
    main()
