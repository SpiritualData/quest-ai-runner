"""Which implementation of each evaluated solution is best, from the committed results.csv.

    .venv/bin/python3 evaluation/components/leaderboard.py [--strict]

Reads results.csv (the latest row per component and implementation, written by ``run.py --record``)
and docs/structure/solutions.csv (which implementation is currently selected). Prints a ranking per
component and flags every challenger that beats the selected implementation: a higher pass rate, or
the same pass rate at a lower cost. ``--strict`` exits 1 when one does, so a pipeline can surface
"a better implementation exists" without a human reading the implementations (ESOI
principle 4). The harness only ranks; switching the live implementation stays a deliberate decision.

A solution whose ``metric_kind`` is ``invariant`` in solutions.csv (a rule that holds or does not, with an
``invariant_reason`` saying why "best" cannot vary) is listed apart, under "Invariant solutions", with its
recorded run and never ranked: there is no challenger to flag. So is a component that carries an ``invariant.txt``
(complementary arms of an evaluated solution).

Only rows with the same ``cases_sha`` as the selected row are compared: a score on a different case
file is not evidence. Rows recorded before ``cases_sha`` existed have it blank and compare as one group,
and are FLAGGED ``unverified``: no case file or code version is pinned to them, so they are shown but never
treated as evidence against a verified row (``comparable`` keeps them apart). Re-record them with
``run.py --record``, or carry an ``S16`` waiver in docs/structure/waivers.csv saying why they cannot be.
"""
import csv
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.csv"
SOLUTIONS = HERE.parents[1] / "docs" / "structure" / "solutions.csv"


def invariant_components(path=SOLUTIONS):
    """{component: invariant_reason} for every component named by the metric_command of an ``invariant`` solution."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("metric_kind") or "").strip() != "invariant":
                continue
            for m in re.finditer(r"components/run\.py (\w+) --impl \w+", row["metric_command"]):
                out[m.group(1)] = row["invariant_reason"].strip()
    return out


def invariant_reason(component, solution_invariants=None):
    """Why the component is not ranked: the ``invariant_reason`` of its invariant solution, or the reason in
    ``<component>/invariant.txt`` when its implementations are complementary (not alternatives). The leaderboard
    shows them but never ranks them or flags a challenger."""
    if solution_invariants and component in solution_invariants:
        return solution_invariants[component]
    note = HERE / component / "invariant.txt"
    return note.read_text().strip() if note.is_file() else ""


def latest_results(path=RESULTS):
    """{component: {impl: row}} keeping the newest row per implementation."""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            prev = out.setdefault(row["component"], {}).get(row["impl"])
            if prev is None or row["date"] >= prev["date"]:
                out[row["component"]][row["impl"]] = row
    return out


def selected_impls(path=SOLUTIONS):
    """{component: selected impl name} for solutions whose metric is one or more harness commands."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            for m in re.finditer(r"components/run\.py (\w+) --impl (\w+)", row["metric_command"]):
                out.setdefault(m.group(1), m.group(2))   # a row may name more than one arm: each is that arm's selection
    return out


def pass_rate(row):
    return int(row["passed"]) / max(int(row["cases"]), 1)


def rank(rows):
    """Best first: pass rate, then cost (lower is better)."""
    return sorted(rows.values(), key=lambda r: (-pass_rate(r), float(r["cost_usd"] or 0), r["impl"]))


def unverified(row):
    """A row recorded without provenance (no ``cases_sha``): shown, flagged, never trusted as evidence."""
    return not (row.get("cases_sha") or "").strip()


def comparable(a, b):
    """Two rows are comparable only when scored on the same case file (blank == blank: legacy rows)."""
    return (a.get("cases_sha") or "") == (b.get("cases_sha") or "")


def is_ablation(row):
    """True when the implementation's module opens with ABLATION: a part of a candidate measured alone (the
    cheap call without the planner behind it). It returns less than the whole job, so it is shown in the
    ranking and never offered as the thing to adopt."""
    path = HERE / row.get("component", "") / "impls" / (row["impl"] + ".py")
    if not path.is_file():
        return False
    return path.read_text().lstrip().startswith('"' * 3 + "ABLATION")


def challengers(rows, selected):
    """Implementations that beat the selected one on pass rate, or tie it at lower cost.
    Rows scored on a different ``cases_sha`` than the selected one are skipped, and so is an unverified
    challenger against a verified selection (and the reverse): provenance-free numbers are not evidence.
    An ABLATION implementation (see ``is_ablation``) is never a challenger."""
    cur = rows.get(selected)
    if cur is None:
        return []
    better = []
    for r in rows.values():
        if r["impl"] == selected or not comparable(r, cur) or is_ablation(r):
            continue
        higher = pass_rate(r) > pass_rate(cur)
        cheaper_tie = pass_rate(r) == pass_rate(cur) and float(r["cost_usd"] or 0) < float(cur["cost_usd"] or 0)
        if higher or cheaper_tie:
            better.append(r)
    return better


def print_rows(rows, sel):
    for r in rank(rows):
        mark = "*" if r["impl"] == sel else " "
        flag = "  [unverified: no cases_sha, recorded before provenance]" if unverified(r) else ""
        print(f"  {mark} {r['impl']:<20} {r['passed']}/{r['cases']} (held-out {r['heldout']}) "
              f"calls={r['model_calls']} cost=${float(r['cost_usd'] or 0):.4f}  {r['date']}{flag}")


def main(argv):
    results, selected = latest_results(RESULTS), selected_impls()
    invariants = invariant_components()
    flagged = 0
    apart = {c: rows for c, rows in results.items() if invariant_reason(c, invariants)}
    for component, rows in sorted(results.items()):
        if component in apart:
            continue
        sel = selected.get(component)
        print(f"{component} (selected: {sel or 'none recorded'})")
        print_rows(rows, sel)
        cur = rows.get(sel)
        for r in rows.values():
            if cur is not None and r["impl"] != sel and not comparable(r, cur):
                print(f"  ? {r['impl']} was scored on different cases (cases_sha {r.get('cases_sha') or 'blank'} "
                      f"vs {cur.get('cases_sha') or 'blank'}): not comparable, re-run both on the same cases")
        for r in challengers(rows, sel):
            flagged += 1
            print(f"  ! {r['impl']} beats the selected {sel}: decide whether to adopt it "
                  f"(update solutions.csv `selected` and the live default together)")
    if apart:
        print("\nInvariant solutions (a rule that holds or does not; listed with their recorded run, never ranked)")
        for component, rows in sorted(apart.items()):
            print(f"{component}: {invariant_reason(component, invariants)}")
            print_rows(rows, selected.get(component))
    return 1 if flagged and "--strict" in argv else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
