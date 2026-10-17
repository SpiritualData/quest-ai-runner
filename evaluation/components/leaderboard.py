"""Which implementation of each evaluated solution is best, from the committed results.csv.

    .venv/bin/python3 evaluation/components/leaderboard.py [--strict]

Reads results.csv (the latest row per component and implementation, written by ``run.py --record``)
and docs/structure/solutions.csv (which implementation is currently selected). Prints a ranking per
component and flags every challenger that beats the selected implementation: a pass rate higher by more than the
stated margin, or a pass rate inside it at clearly lower spend. ``--strict`` exits 1 when one does, so a pipeline can surface
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
import json
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


def solution_rows(path=SOLUTIONS):
    """The rows of solutions.csv, each with the `metric_command` of its metric records (metrics.csv beside it,
    linked by `metric_ids`); a registry without metrics.csv keeps the column it has."""
    path = Path(path)
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    metrics_file = path.with_name("metrics.csv")
    if metrics_file.exists():
        with open(metrics_file, newline="") as fh:
            commands = {m["metric_id"]: m["command"] for m in csv.DictReader(fh)}
        for row in rows:
            row["metric_command"] = "; ".join(commands[i] for i in row.get("metric_ids", "").split(";") if i in commands)
    return rows


def invariant_components(path=SOLUTIONS):
    """{component: invariant_reason} for every component named by the metric_command of an ``invariant`` solution."""
    out = {}
    for row in solution_rows(path):
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
    for row in solution_rows(path):
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


ARM_MARKER = re.compile(r'\s*"""\s*(ABLATION|BASELINE|FLOOR|ALTERNATIVE|VALIDITY)')
NOT_RIVALS = ("ablation", "validity")   # shown and tagged, never offered as the thing to adopt


def arm_kind(row):
    """What an implementation is for, from the first word of its module docstring: ``ablation`` (a part of a
    candidate measured alone: the cheap call without the planner behind it), ``baseline`` (a floor someone
    would plausibly ship; ``FLOOR`` counts), ``validity`` (the metric's own offline check that it can tell good from
    bad, which no implementation can lose to), ``alternative``, or ``""`` for live code. Shown beside every row so a
    reader can tell a rival from a reference point."""
    path = HERE / row.get("component", "") / "impls" / (row["impl"] + ".py")
    if not path.is_file():
        return ""
    found = ARM_MARKER.match(path.read_text())
    if not found:
        return ""
    return {"FLOOR": "baseline"}.get(found.group(1), found.group(1).lower())


def is_ablation(row):
    """True when the implementation is no rival: an ABLATION returns less than the whole job, and a VALIDITY arm
    checks the metric rather than competing on it, so both are shown in the ranking and never offered as the
    thing to adopt."""
    return arm_kind(row) in NOT_RIVALS


COST_TIE_TOLERANCE = 0.10   # spend within 10% is a tie for deterministic runs: noise must not flip a selection
SAMPLED_PASS_MARGIN = 0.10  # sampled, model-judged runs: pass rates within 10 percentage points are a tie
SAMPLED_SPEND_TOLERANCE = 0.15  # and spend within 15% is a tie (one pair's tokens moved 6.5% to 11% between re-runs)


def is_sampled(row):
    """A run that called a model: its pass rate and tokens move between re-runs of the same code, so a small gap
    between two such rows is noise, not a finding."""
    return int(float(row.get("model_calls") or 0)) > 0


def noise(row, other):
    """``(pass_margin, spend_tolerance)`` for comparing two rows: a fraction of pass rate, and a fraction of spend,
    inside which they are a TIE. Deterministic runs (no model call on either side) keep exact pass counts and the
    cost tolerance. A sampled comparison uses the defaults above unless the component folder carries a
    ``noise.json`` such as ``{"pass_margin": 0.15, "spend_tolerance": 0.2}`` (its own measured re-run swing)."""
    if not (is_sampled(row) or is_sampled(other)):
        return 0.0, COST_TIE_TOLERANCE
    margin, tolerance = SAMPLED_PASS_MARGIN, SAMPLED_SPEND_TOLERANCE
    component = row.get("component") or other.get("component") or ""
    setting = HERE / component / "noise.json"
    if component and setting.is_file():
        found = json.loads(setting.read_text())
        margin = float(found.get("pass_margin", margin))
        tolerance = float(found.get("spend_tolerance", tolerance))
    return margin, tolerance


def spend(row):
    """What a run consumed. Tokens when the run reports them (the CLI-reported dollars swing with prompt
    caching), else the recorded dollars."""
    tokens = float(row.get("tokens_in") or 0) + float(row.get("tokens_out") or 0)
    return ("tokens", tokens) if tokens > 0 else ("usd", float(row.get("cost_usd") or 0))


def clearly_cheaper(row, other):
    """True when ``row`` spent more than the spend tolerance (see ``noise``) less than ``other``, in the same unit."""
    (unit, mine), (other_unit, theirs) = spend(row), spend(other)
    if unit != other_unit:
        unit, (mine, theirs) = "usd", (float(row.get("cost_usd") or 0), float(other.get("cost_usd") or 0))
    return mine < theirs * (1 - noise(row, other)[1])


def clearly_higher(row, other):
    """True when ``row`` passes more than the pass margin (see ``noise``) above ``other``."""
    return Fraction(pass_rate(row)).limit_denominator(10000) - Fraction(pass_rate(other)).limit_denominator(10000) \
        > Fraction(noise(row, other)[0]).limit_denominator(10000)


def cheaper_without_passing_less(row, other):
    """Spend settles a comparison only when the cheaper side passes at least as much: inside the pass margin a lower
    pass rate at lower spend is not a finding (it may be a worse implementation, and the margin cannot say)."""
    return pass_rate(row) >= pass_rate(other) and clearly_cheaper(row, other)


def challengers(rows, selected):
    """Implementations that beat the selected one on pass rate by more than the pass margin, or stay inside the
    margin at clearly lower spend without passing less (more than the spend tolerance below it: see ``noise`` and ``spend``).
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
        if clearly_higher(r, cur) or (not clearly_higher(cur, r) and cheaper_without_passing_less(r, cur)):
            better.append(r)
    return better


def tag(row):
    kind = arm_kind(row)
    return f"  [{kind.upper()}]" if kind else ""


def tokens_label(row):
    """Tokens in and out, compact (``1.29M``, ``610k``, ``0``): the number spend verdicts are decided on."""
    total = float(row.get("tokens_in") or 0) + float(row.get("tokens_out") or 0)
    return f"{total / 1e6:.2f}M" if total >= 1e6 else f"{total / 1e3:.0f}k" if total >= 1e3 else f"{total:.0f}"


def print_rows(rows, sel):
    for r in rank(rows):
        mark = "*" if r["impl"] == sel else " "
        flag = "  [unverified: no cases_sha, recorded before provenance]" if unverified(r) else ""
        print(f"  {mark} {r['impl']:<20} {r['passed']}/{r['cases']} (held-out {r['heldout']}) "
              f"calls={r['model_calls']} tokens={tokens_label(r)} cost=${float(r['cost_usd'] or 0):.4f}  "
              f"{r['date']}{tag(r)}{flag}")
        if (r.get("details") or "").strip():
            print(f"      per-case reasons: {r['details'].strip()}")


def verdict(rows, selected):
    """How the selected implementation stands against the other measured ones, as ``(label, detail)``:

    * ``WIN``: it beats every rival on the same cases, by pass rate or by clearly lower spend at the same rate;
    * ``TIE``: some rival is inside the noise (``noise``: exact pass counts and the cost tolerance for deterministic
      runs; a pass-rate margin and a spend tolerance when a model was called, because sampled, model-judged runs move
      between re-runs): the metric did not separate them, so the selection is kept for a reason outside the number
      (``selected_by``), not decided by it;
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
    tied = []
    for r in rivals:
        if clearly_higher(cur, r) or cheaper_without_passing_less(cur, r):
            continue
        margin, tolerance = noise(cur, r)
        tied.append((r["impl"], margin, tolerance))
    if tied:
        names = ", ".join(sorted(t[0] for t in tied))
        margin, tolerance = max(t[1] for t in tied), max(t[2] for t in tied)
        if margin:
            return "TIE", (f"within the stated margin of {names}: pass rate inside {margin * 100:.0f} percentage "
                           f"points and spend inside {tolerance * 100:.0f}% (sampled, model-judged runs move that "
                           "much between re-runs), so the metric does not separate them")
        return "TIE", (f"passes as many cases as {names} at spend within {tolerance * 100:.0f}% (or lower), so the "
                       "metric does not separate them")
    reasons = []
    for r in sorted(rivals, key=lambda r: r["impl"]):
        if clearly_higher(cur, r):
            reasons.append(f"passes more cases than {r['impl']}"
                           + (f" by more than the {noise(cur, r)[0] * 100:.0f} point margin" if noise(cur, r)[0] else ""))
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
    for row in solution_rows(path):
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
