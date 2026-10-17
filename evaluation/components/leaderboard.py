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
import hashlib
import re
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.csv"
SOLUTIONS = HERE.parents[1] / "docs" / "structure" / "solutions.csv"


def cases_files(component, base=HERE):
    """The case files a component is scored on: ``cases.json`` (or ``cases.csv`` when the dataset is reviewed as a
    sheet), plus every ``datasets/*.json`` of a component whose cases live in a dataset folder."""
    folder = Path(base) / component
    main = next((folder / n for n in ("cases.json", "cases.csv") if (folder / n).is_file()), None)
    return ([main] if main else []) + sorted((folder / "datasets").glob("*.json"))


def cases_digest(component, base=HERE):
    """Short hash of everything ``cases_files`` names: the ``cases_sha`` a recorded run is pinned to. Line endings
    are normalised first, because a dataset reviewed as a sheet comes back from the sheet with either kind."""
    files = cases_files(component, base)
    if not files:
        return ""
    return hashlib.sha256(b"".join(f.read_bytes().replace(b"\r\n", b"\n") for f in files)).hexdigest()[:8]


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


ARM_MARKER = re.compile(r'\s*"""\s*(ABLATION|BASELINE|FLOOR|ALTERNATIVE)')


def arm_kind(row):
    """What an implementation is for, from the first word of its module docstring: ``ablation`` (a part of a
    candidate measured alone: the cheap call without the planner behind it), ``baseline`` (a floor someone
    would plausibly ship; ``FLOOR`` counts), ``alternative``, or ``""`` for live code. Shown beside every row so a
    reader can tell a rival from a reference point."""
    path = HERE / row.get("component", "") / "impls" / (row["impl"] + ".py")
    if not path.is_file():
        return ""
    found = ARM_MARKER.match(path.read_text())
    if not found:
        return ""
    return {"FLOOR": "baseline"}.get(found.group(1), found.group(1).lower())


def is_ablation(row):
    """True when the implementation's module opens with ABLATION: it returns less than the whole job, so it is
    shown in the ranking and never offered as the thing to adopt."""
    return arm_kind(row) == "ablation"


COST_TIE_TOLERANCE = 0.10  # costs within 10% are a tie: run-to-run noise must not flip a selection


def spend(row):
    """What a run consumed. Tokens when the run reports them (the CLI-reported dollars swing with prompt
    caching), else the recorded dollars."""
    tokens = float(row.get("tokens_in") or 0) + float(row.get("tokens_out") or 0)
    return ("tokens", tokens) if tokens > 0 else ("usd", float(row.get("cost_usd") or 0))


def clearly_cheaper(row, other):
    """True when ``row`` spent more than COST_TIE_TOLERANCE less than ``other``, in the same unit."""
    (unit, mine), (other_unit, theirs) = spend(row), spend(other)
    if unit != other_unit:
        unit, (mine, theirs) = "usd", (float(row.get("cost_usd") or 0), float(other.get("cost_usd") or 0))
    return mine < theirs * (1 - COST_TIE_TOLERANCE)


def challengers(rows, selected):
    """Implementations that beat the selected one on pass rate, or tie it at clearly lower spend (more than
    COST_TIE_TOLERANCE below it: see ``spend``).
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
        cheaper_tie = pass_rate(r) == pass_rate(cur) and clearly_cheaper(r, cur)
        if higher or cheaper_tie:
            better.append(r)
    return better


def tag(row):
    kind = arm_kind(row)
    return f"  [{kind.upper()}]" if kind else ""


def print_rows(rows, sel):
    for r in rank(rows):
        mark = "*" if r["impl"] == sel else " "
        flag = "  [unverified: no cases_sha, recorded before provenance]" if unverified(r) else ""
        print(f"  {mark} {r['impl']:<20} {r['passed']}/{r['cases']} (held-out {r['heldout']}) "
              f"calls={r['model_calls']} cost=${float(r['cost_usd'] or 0):.4f}  {r['date']}{tag(r)}{flag}")


def verdict(rows, selected):
    """How the selected implementation stands against the other measured ones, as ``(label, detail)``:

    * ``WIN``: it beats every rival on the same cases, by pass rate or by clearly lower spend at the same rate;
    * ``TIE``: some rival passes as many cases at spend within COST_TIE_TOLERANCE (or lower): the metric did not
      separate them, so the selection is kept for a reason outside the number (``selected_by``), not decided by it;
    * ``LOSS``: a rival beats it (see ``challengers``);
    * ``UNMEASURED``: it has no row, or no rival was measured on the same cases, so nothing was compared.

    Ablations are not rivals (they return less than the whole job); baselines are."""
    cur = rows.get(selected)
    if cur is None:
        return "UNMEASURED", "the selected implementation has no recorded row"
    rivals = [r for r in rows.values() if r["impl"] != selected and comparable(r, cur) and not is_ablation(r)]
    if not rivals:
        return "UNMEASURED", "no rival implementation was measured on the same cases"
    beaten_by = [r["impl"] for r in challengers(rows, selected)]
    if beaten_by:
        return "LOSS", "beaten by " + ", ".join(sorted(beaten_by))
    mine = Fraction(int(cur["passed"]), max(int(cur["cases"]), 1))
    tied = [r["impl"] for r in rivals
            if Fraction(int(r["passed"]), max(int(r["cases"]), 1)) == mine and not clearly_cheaper(cur, r)]
    if tied:
        return "TIE", (f"passes as many cases as {', '.join(sorted(tied))} at spend within "
                       f"{int(COST_TIE_TOLERANCE * 100)}% (or lower), so the metric does not separate them")
    reasons = []
    for r in sorted(rivals, key=lambda r: r["impl"]):
        if Fraction(int(r["passed"]), max(int(r["cases"]), 1)) < mine:
            reasons.append(f"passes more cases than {r['impl']}")
        else:
            (_, theirs), (_, ours) = spend(r), spend(cur)
            saved = 1 - ours / theirs if theirs and spend(r)[0] == spend(cur)[0] else 0.0
            reasons.append(f"same pass rate as {r['impl']} at {saved:.0%} lower spend" if saved
                           else f"same pass rate as {r['impl']} at lower spend")
    return "WIN", "; ".join(reasons)


def solution_verdicts(results=None, path=SOLUTIONS):
    """{solution_id: {"verdict", "detail", "component", "selected_by"}} for every ``eval`` solution, judged on the
    first harness arm of its ``metric_command`` (the primary metric; later arms are listed on the board)."""
    results = latest_results(RESULTS) if results is None else results
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("metric_kind") or "").strip() != "eval":
                continue
            m = re.search(r"components/run\.py (\w+) --impl (\w+)", row["metric_command"])
            if not m:
                out[row["solution_id"]] = dict(verdict="UNMEASURED", detail="no harness metric", component="",
                                               selected_by=row["selected_by"])
                continue
            label, detail = verdict(results.get(m.group(1), {}), m.group(2))
            out[row["solution_id"]] = dict(verdict=label, detail=detail, component=m.group(1),
                                           selected_by=row["selected_by"])
    return out


def main(argv):
    results, selected = latest_results(RESULTS), selected_impls()
    invariants = invariant_components()
    flagged = 0
    apart = {c: rows for c, rows in results.items() if invariant_reason(c, invariants)}
    for component, rows in sorted(results.items()):
        if component in apart:
            continue
        sel = selected.get(component)
        label, detail = verdict(rows, sel) if sel else ("UNMEASURED", "no selected implementation")
        print(f"{component} (selected: {sel or 'none recorded'}) {label}: {detail}")
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
    print("\nBy solution (WIN = the metric chose it; TIE = kept for the reason in selected_by, the metric did not "
          "separate it; UNMEASURED = no rival was compared)")
    for sid, v in sorted(solution_verdicts(results).items()):
        reason = f"  kept because: {v['selected_by']}" if v["verdict"] == "TIE" else ""
        print(f"  {v['verdict']:<10} {sid}: {v['detail']}{reason}")
    if apart:
        print("\nInvariant solutions (a rule that holds or does not; listed with their recorded run, never ranked)")
        for component, rows in sorted(apart.items()):
            print(f"{component}: {invariant_reason(component, invariants)}")
            print_rows(rows, selected.get(component))
    return 1 if flagged and "--strict" in argv else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
