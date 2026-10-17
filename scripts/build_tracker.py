#!/usr/bin/env python3
"""Build the ESOI tracker tabs (problems, solutions, implementations, ideals, metrics) in
docs/structure/tracker/ from the registry (solutions.csv, components.csv, principles.csv) and the
recorded evaluation results. It is the optional Sheet view: nothing else reads the tabs, and check S17 only
keeps them in step with the registry.

Each tab is a CSV, importable as one tab of a Google Sheet. Structural columns are regenerated and
must match the registry (check S17); the `tracking_*` columns are the people's and the AI's own notes
and are preserved across rebuilds (blank means "use auto_status").

    python scripts/build_tracker.py            # rewrite the tabs, keeping tracking_* columns
    python scripts/build_tracker.py --check    # exit 1 when a tab is out of date
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRUCT = ROOT / "docs" / "structure"
TRACKER = STRUCT / "tracker"
TRACKING = ["tracking_status", "tracking_owner", "tracking_updated", "tracking_notes"]
ALTERNATIVE_MARKERS = ("ALTERNATIVE", "BASELINE", "ABLATION", "FLOOR", "VALIDITY")
SEP = ";"



def load_records():
    """quest_ai_runner/structure/records.py (standard library only) owns the template reader."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qar_structure_records", ROOT / "quest_ai_runner" / "structure" / "records.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


records = load_records()
TEMPLATE = records.load_template(STRUCT / "esoi_template.csv")


def template_columns(entity: str) -> list[str]:
    """The record fields the evolvable-structure template lists for an entity (esoi_template.csv)."""
    return records.fields(TEMPLATE, entity, interface_required=True)


COLUMNS = {
    "problems": ["problem_id", "overall_problem_id"] + template_columns("problem") + ["solution_ids", "ideal_ids", "component_ids", "auto_status"],
    "solutions": ["solution_id", "area"] + template_columns("solution") + ["problem_ids", "ideal_ids", "metric_ids", "implementation_ids",
                  "port", "selected_implementation_id", "selected_by", "metric_kind", "verdict", "verdict_detail",
                  "component_ids", "auto_status"],
    "implementations": ["implementation_id", "solution_id", "path"] + template_columns("implementation") + [
                        "kind", "selected", "latest_result", "latest_cost_usd", "cases_sha", "auto_status"],
    "ideals": ["ideal_id", "overall_ideal_id", "scope"] + template_columns("ideal") + ["problem_ids", "solution_ids", "metric_ids", "auto_status"],
    "metrics": ["metric_id"] + template_columns("metric") + ["solution_ids", "command", "kind", "latest_result", "gap",
                "action", "auto_status"],
}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _stem(path: str) -> str:
    return re.sub(r"\.py$", "", path.rsplit("/", 1)[-1])


def _leaderboard():
    """evaluation/components/leaderboard.py is the one ranking implementation; the tracker reads its verdicts."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qar_leaderboard_for_tracker",
                                                  ROOT / "evaluation" / "components" / "leaderboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# What a solution's verdict means for its status (the leaderboard decides; see leaderboard.verdict).
STATUS_BY_VERDICT = {"WIN": "decided-by-metric", "TIE": "tie-kept-by-selected_by", "LOSS": "beaten-by-rival",
                     "UNMEASURED": "no-rival-measured"}


def _kind(path: str) -> str:
    """ablation / baseline / alternative / live for an implementation path (documentation, not a gate)."""
    found = re.match(r"evaluation/components/(\w+)/impls/(\w+)\.py$", path)
    if found:
        tagged = _leaderboard().arm_kind({"component": found.group(1), "impl": found.group(2)})
        if tagged:
            return tagged
    full = ROOT / path
    if not full.suffix == ".py" or not full.exists():
        return "live"
    head = full.read_text(errors="replace")[:600].lstrip().lstrip('"').lstrip().upper()
    if any(head.startswith(m) for m in ALTERNATIVE_MARKERS) or re.search(r"^ALTERNATIVE\s*=\s*True", full.read_text(errors="replace"), re.M):
        return "alternative"
    return "live"


def selection_table() -> dict:
    """core/selection_table.py is pure data, so it is loaded on its own (no package import)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qar_selection_table", ROOT / "quest_ai_runner" / "core" / "selection_table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SELECTION


def selected_path(solution: dict, table: dict) -> str:
    """The implementation path a solution's `selected` cell stands for: a selection-table name is resolved to
    its module file, any other value is already a path."""
    entry = table.get(solution["solution_id"])
    if not entry:
        return solution["selected"]
    ref = entry["implementations"].get(solution["selected"])
    if ref is None:
        return solution["selected"]
    target = ref["target"] if isinstance(ref, dict) else ref
    return target.partition(":")[0].replace(".", "/") + ".py"


def _latest_results() -> dict:
    out = {}
    for row in read_csv(ROOT / "evaluation" / "components" / "results.csv"):
        out[(row["component"], row["impl"])] = row
    return out


def _command_target(command: str) -> tuple[str, str]:
    m = re.search(r"run\.py\s+(\w+)\s+--impl\s+(\w+)", command or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def build() -> dict[str, list[dict]]:
    """The five tabs, from the first-class records (problems, solutions, implementations, ideals, metrics)."""
    solutions = read_csv(STRUCT / "solutions.csv")
    components = read_csv(STRUCT / "components.csv")
    principles = read_csv(STRUCT / "principles.csv")
    problem_records = read_csv(STRUCT / "problems.csv")
    ideal_records = read_csv(STRUCT / "ideals.csv")
    impl_records = {r["implementation_id"]: r for r in read_csv(STRUCT / "implementations.csv")}
    metric_records = read_csv(STRUCT / "metrics.csv")
    metric_by_id = {m["metric_id"]: m for m in metric_records}
    results = _latest_results()
    verdicts = _leaderboard().solution_verdicts()
    table = selection_table()
    by_solution: dict[str, list[str]] = {}
    for comp in components:
        for sid in filter(None, comp["solutions"].split(SEP)):
            by_solution.setdefault(sid, []).append(comp["component_id"])
    sol_by_id = {s["solution_id"]: s for s in solutions}

    def command_of(s: dict) -> str:
        return SEP.join(metric_by_id[m]["command"] for m in filter(None, s["metric_ids"].split(SEP)) if m in metric_by_id)

    problems, sol_rows, impls, ideals, metrics = [], [], [], [], []
    for p in problem_records:
        linked = [sol_by_id[s] for s in filter(None, p["solution_ids"].split(SEP)) if s in sol_by_id]
        open_ones = [s["solution_id"] for s in linked if s["gap"]]
        problems.append({**{k: p.get(k, "") for k in COLUMNS["problems"] if k not in ("component_ids", "auto_status")},
                         "component_ids": SEP.join(sorted({c for s in linked for c in by_solution.get(s["solution_id"], [])})),
                         "auto_status": "open: gap on " + ", ".join(open_ones) if open_ones else "served"})
    for s in solutions:
        sid = s["solution_id"]
        command = command_of(s)
        has_metric = command not in ("", "none")
        comp, _ = _command_target(command)
        impl_ids, selected_id = [], ""
        chosen = selected_path(s, table)
        for path in filter(None, s["implementations"].split(SEP)):
            iid = f"{sid}:{_stem(path)}"
            impl_ids.append(iid)
            is_sel = path == chosen
            selected_id = iid if is_sel else selected_id
            res = results.get((comp, _stem(path))) if comp else None
            rec = impl_records.get(iid, {})
            impls.append({
                "implementation_id": iid, "solution_id": sid, "path": path,
                **{k: rec.get(k, "") for k in template_columns("implementation")}, "kind": _kind(path),
                "selected": "yes" if is_sel else "",
                "latest_result": f"{res['passed']}/{res['cases']}" if res else "",
                "latest_cost_usd": res["cost_usd"] if res else "",
                "cases_sha": res["cases_sha"] if res else "",
                "auto_status": "selected" if is_sel else ("measured" if res else "unmeasured")})
        sel_res = results.get((comp, _command_target(command)[1])) if comp else None
        sol_rows.append({
            **{k: s.get(k, "") for k in ["solution_id", "area"] + template_columns("solution") + ["problem_ids", "ideal_ids", "metric_ids", "port",
                                                                                          "selected_by", "metric_kind"]},
            "implementation_ids": SEP.join(impl_ids), "selected_implementation_id": selected_id,
            "verdict": verdicts.get(sid, {}).get("verdict", ""),
            "verdict_detail": verdicts.get(sid, {}).get("detail", ""),
            "component_ids": SEP.join(by_solution.get(sid, [])),
            "auto_status": "invariant" if s["metric_kind"] == "invariant"
            else (STATUS_BY_VERDICT.get(verdicts.get(sid, {}).get("verdict", ""), "decided-by-hand")
                  if sel_res else "decided-by-hand")})
        for mid in filter(None, s["metric_ids"].split(SEP)):
            m = metric_by_id.get(mid)
            if m is None:
                continue
            metrics.append({
                **{k: m.get(k, "") for k in ["metric_id"] + template_columns("metric") + ["solution_ids", "command"]},
                "kind": s["metric_kind"] or ("eval" if has_metric else "none"),
                "latest_result": f"{sel_res['passed']}/{sel_res['cases']}" if sel_res else "",
                "gap": s["gap"], "action": s["action"],
                "auto_status": "measured" if sel_res else ("needs_run" if has_metric else "needs_metric")})
    solutions_of_ideal: dict[str, list[str]] = {}
    for s in solutions:
        for iid in filter(None, s["ideal_ids"].split(SEP)):
            solutions_of_ideal.setdefault(iid, []).append(s["solution_id"])
    for i in ideal_records:
        sids = solutions_of_ideal.get(i["ideal_id"], [])
        measured = any(_command_target(command_of(sol_by_id[sid]))[0] for sid in sids)
        ideals.append({**{k: i.get(k, "") for k in ["ideal_id", "overall_ideal_id"] + template_columns("ideal") + ["problem_ids"]}, "scope": "solution",
                       "solution_ids": SEP.join(sids),
                       "metric_ids": SEP.join(m for sid in sids for m in sol_by_id[sid]["metric_ids"].split(SEP) if m),
                       "auto_status": "measured" if measured else "unmeasured"})
    for p in principles:
        ideals.append({"ideal_id": p["principle_id"].lower(), "scope": "principle", "name": p["name"], "principle": p["statement"],
                       "state_description": "", "hints_of_measurement": p["how_measured"], "problem_ids": "",
                       "solution_ids": "the whole repo", "metric_ids": "",
                       "auto_status": "enforced-by-check" if "(S" in p["how_measured"] else "behaviour-eval"})
    return {"problems": problems, "solutions": sol_rows, "implementations": impls,
            "ideals": ideals, "metrics": metrics}


def _merge(tab: str, rows: list[dict]) -> list[dict]:
    key = COLUMNS[tab][0]
    old = {r[key]: r for r in read_csv(TRACKER / f"{tab}.csv")}
    for r in rows:
        for col in TRACKING:
            r[col] = old.get(r[key], {}).get(col, "")
    return rows


def render(tab: str, rows: list[dict]) -> str:
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS[tab] + TRACKING, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def stale() -> list[str]:
    """Tabs whose structural columns differ from what the registry now produces."""
    out = []
    for tab, rows in build().items():
        have = read_csv(TRACKER / f"{tab}.csv")
        want = _merge(tab, rows)
        if [{k: r.get(k, "") for k in COLUMNS[tab]} for r in have] != \
                [{k: r.get(k, "") for k in COLUMNS[tab]} for r in want] or \
                (have and list(have[0].keys()) != COLUMNS[tab] + TRACKING):
            out.append(tab)
    return out


def main(argv: list[str]) -> int:
    if "--check" in argv:
        bad = stale()
        print("tracker out of date: " + ", ".join(bad) if bad else "tracker ok")
        return 1 if bad else 0
    TRACKER.mkdir(parents=True, exist_ok=True)
    for tab, rows in build().items():
        (TRACKER / f"{tab}.csv").write_text(render(tab, _merge(tab, rows)))
        print(f"{tab}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
