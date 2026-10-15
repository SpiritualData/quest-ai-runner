#!/usr/bin/env python3
"""Check that the repo still follows the adaptable-structure rules in docs/structure/.

Run from anywhere:  python scripts/check_structure.py [--write-docs]
Exit 0 = clean, 1 = violations (printed one per line as ``RULE path: message``).

The registry is data, not code (see docs/structure/README.md):
  * components.csv   every functional component: path globs, role, interface, metric, docs, tests, gap
  * solutions.csv    each swappable solution: problem, ideal, metric, implementations, selected one
  * waivers.csv      known, justified exceptions to a structure rule (each must still apply)
  * COMPONENTS.md    generated from components.csv (--write-docs rewrites it)

Rules checked:
  S1 coverage    every file belongs to exactly one component; every component glob matches a file
  S2 component   docs and tests named by a component exist; unfinished components state gap and action
  S3 root        no .py file at the repo root
  S4 size        no module over MAX_LINES lines (waiver otherwise)
  S5 docstring   every library module opens with a docstring that states its role
  S6 direction   dependencies point inward: core imports no adapters/runner/config/cli;
                 adapters import no runner/config/cli (waiver otherwise)
  S7 solutions   every port in core/adapters.py has a solutions.csv row; implementations exist
  S8 generated   COMPONENTS.md matches components.csv
  S9 waivers     every waiver still applies (the list can only shrink)
  S10 selection  the selected implementation of a measured solution is its leaderboard winner,
                 or a waiver names the pending decision; a selected harness implementation has a
                 recorded result
  S11 metric     a `done` component has a metric kind (eval with a recorded result, invariant
                 pinned by tests, or exempt with a reason); a harness command names a real
                 component and implementation; every component solution id exists
"""
from __future__ import annotations

import ast
import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRUCT = ROOT / "docs" / "structure"
MAX_LINES = 1500
PKG = "quest_ai_runner"
STATUSES = {"done", "partial", "waived", "open"}
# layer -> package prefixes it must not import (dependencies point inward)
# a prefix matches the package or any module that starts with it (``config`` also bans ``config_resolvers``)
FORBIDDEN = {
    "core": ("adapters", "runner", "config", "cli", "tools", "interactive_session", "textual_ui"),
    "adapters": ("runner", "config", "cli", "tools", "interactive_session", "textual_ui"),
    "runner": ("cli", "interactive_session", "textual_ui"),
}
METRIC_KINDS = {"eval", "invariant", "exempt", "none"}
HARNESS = re.compile(r"components/run\.py (\w+) --impl (\w+)")
COMPONENT_COLS = ["component_id", "layer", "path", "role", "interface", "metric", "docs", "tests",
                  "rules", "gap", "action", "status", "metric_kind", "solutions"]
SOLUTION_COLS = ["solution_id", "problem", "ideal", "port", "implementations", "selected",
                 "selected_by", "metric", "metric_command", "gap", "action"]


def _load_leaderboard():
    """evaluation/components/leaderboard.py is the one ranking implementation; the check reuses it."""
    import importlib.util
    path = ROOT / "evaluation" / "components" / "leaderboard.py"
    spec = importlib.util.spec_from_file_location("qar_leaderboard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


leaderboard = _load_leaderboard()


def latest_results():
    return leaderboard.latest_results(ROOT / "evaluation" / "components" / "results.csv")


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def glob_re(pattern: str) -> re.Pattern:
    """`*` stays inside a path segment, `**` crosses segments."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def repo_files() -> list[str]:
    cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(f for f in set(out.splitlines()) if (ROOT / f).is_file())


def patterns(cell: str) -> list[str]:
    return [p.strip() for p in cell.split(";") if p.strip()]


def module_imports(path: Path, dotted: str) -> list[tuple[int, str]]:
    """(line, top-level package under quest_ai_runner) for each import, lazy ones included."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    parts = dotted.split(".")
    pkg_parts = parts if path.name == "__init__.py" else parts[:-1]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                full = base + (node.module.split(".") if node.module else [])
                names = [a.name for a in node.names] if not node.module else []
            else:
                full = (node.module or "").split(".")
                names = []
            if full[:1] == [PKG] and len(full) > 1:
                found.append((node.lineno, full[1]))
            elif full == [PKG]:
                found.extend((node.lineno, n) for n in names)
            elif node.level and len(full) == 1 and not node.module:
                found.extend((node.lineno, n) for n in names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                p = a.name.split(".")
                if p[0] == PKG and len(p) > 1:
                    found.append((node.lineno, p[1]))
    return found


def render_docs(components: list[dict]) -> str:
    lines = [
        "# Components",
        "",
        "Generated from `docs/structure/components.csv` by `python scripts/check_structure.py --write-docs`.",
        "Edit the CSV, never this file. Rules are the numbered ideals in `docs/structure/README.md`.",
        "",
        "| Component | Layer | Role | Interface | Metric | Docs | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in components:
        cell = lambda k: r[k].replace("|", "/").replace(";", ", ")
        lines.append(f"| `{r['component_id']}` | {r['layer']} | {cell('role')} | {cell('interface')} | "
                     f"{cell('metric')} | {cell('docs')} | {r['status']} |")
    open_rows = [r for r in components if r["status"] != "done"]
    if open_rows:
        lines += ["", "## Open gaps", ""]
        for r in open_rows:
            lines.append(f"* `{r['component_id']}` ({r['status']}): {r['gap']} Action: {r['action']}")
    return "\n".join(lines) + "\n"


def check() -> list[str]:
    bad: list[str] = []
    components = read_csv(STRUCT / "components.csv")
    solutions = read_csv(STRUCT / "solutions.csv")
    waivers = read_csv(STRUCT / "waivers.csv")
    files = repo_files()
    used_waivers: set[tuple[str, str]] = set()

    def waived(rule: str, key: str) -> bool:
        for w in waivers:
            if w["rule"] == rule and w["path"] == key:
                used_waivers.add((rule, key))
                return True
        return False

    # S1 coverage
    seen_ids: set[str] = set()
    owners: dict[str, list[str]] = {f: [] for f in files}
    for r in components:
        cid = r["component_id"]
        if cid in seen_ids:
            bad.append(f"S1 {cid}: duplicate component_id")
        seen_ids.add(cid)
        if r["status"] not in STATUSES:
            bad.append(f"S2 {cid}: status {r['status']!r} not in {sorted(STATUSES)}")
        for p in patterns(r["path"]):
            rx = glob_re(p)
            hit = [f for f in files if rx.match(f)]
            if not hit:
                bad.append(f"S1 {cid}: path pattern {p!r} matches no file")
            for f in hit:
                if cid not in owners[f]:
                    owners[f].append(cid)
        for k in ("role", "interface", "metric", "docs", "tests", "rules"):
            if not r[k].strip():
                bad.append(f"S2 {cid}: column {k} is empty")
        if r["status"] != "done" and not (r["gap"].strip() and r["action"].strip()):
            bad.append(f"S2 {cid}: status {r['status']} needs both gap and action")
        for d in patterns(r["docs"]):
            if not (ROOT / d).exists():
                bad.append(f"S2 {cid}: doc {d} does not exist")
        for t in patterns(r["tests"]):
            if t.startswith("none:"):
                continue
            rx = glob_re(t)
            if not any(rx.match(f) for f in files):
                bad.append(f"S2 {cid}: test pattern {t!r} matches no file")
    for f, ids in owners.items():
        if not ids:
            bad.append(f"S1 {f}: belongs to no component (add it to docs/structure/components.csv)")
        elif len(ids) > 1:
            bad.append(f"S1 {f}: belongs to several components {ids}")

    # S3 root
    for f in files:
        if "/" not in f and f.endswith(".py"):
            bad.append(f"S3 {f}: .py file at the repo root (use tests/, scripts/ or the package)")

    # S4 size, S5 docstring, S6 direction
    for f in files:
        if not f.endswith(".py"):
            continue
        path = ROOT / f
        if not path.exists():
            continue
        n = sum(1 for _ in path.open())
        if n > MAX_LINES and not waived("S4", f):
            bad.append(f"S4 {f}: {n} lines (max {MAX_LINES}); split by role or add a waiver")
        if f.startswith(PKG + "/") and "/vendor/" not in f:
            try:
                doc = ast.get_docstring(ast.parse(path.read_text()))
            except SyntaxError:
                doc = None
            if path.stat().st_size and not doc:
                bad.append(f"S5 {f}: module has no docstring stating its role")
            dotted = f[:-3].replace("/", ".")
            segs = f.split("/")
            layer = segs[1] if len(segs) > 2 else ""
            for line, top in module_imports(path, dotted):
                if any(top == b or top.startswith(b + "_") for b in FORBIDDEN.get(layer, ())) \
                        and not waived("S6", f"{f}:{top}"):
                    bad.append(f"S6 {f}:{line}: {layer} imports {top} (dependencies point inward)")

    # S7 solutions
    ports: set[str] = set()
    for port_file in sorted((ROOT / PKG).rglob("*.py")):
        if "/vendor/" in str(port_file):
            continue
        ports.update(n for n in re.findall(r"^class (\w+)\((?:[\w, ]*)Protocol", port_file.read_text(), re.M)
                     if not n.startswith("_"))
    covered = set()
    for r in solutions:
        sid = r["solution_id"]
        covered.update(patterns(r["port"]))
        impls = patterns(r["implementations"])
        if r["selected"] and r["selected"] not in impls:
            bad.append(f"S7 {sid}: selected {r['selected']!r} is not among its implementations")
        for i in impls:
            if "/" in i and not (ROOT / i.split("::")[0]).exists():
                bad.append(f"S7 {sid}: implementation path {i} does not exist")
        mc = r["metric_command"].strip()
        hm = HARNESS.search(mc)
        if hm:
            comp, impl = hm.groups()
            base = ROOT / "evaluation" / "components"
            if not (base / comp / "score.py").exists() or not (base / comp / "impls" / f"{impl}.py").exists():
                bad.append(f"S7 {sid}: harness command names {comp}/{impl}, which has no score.py or impls file")
        if mc and not mc.startswith("none"):
            tgt = mc.split()[0] if not mc.startswith("python") else (mc.split()[1] if len(mc.split()) > 1 else "")
            if tgt.endswith(".py") and not (ROOT / tgt).exists():
                bad.append(f"S7 {sid}: metric script {tgt} does not exist")
        elif not (r["gap"].strip() and r["action"].strip()):
            bad.append(f"S7 {sid}: no metric, so gap and action are required")
    for p in sorted(ports - covered):
        bad.append(f"S7 {p}: port in core/adapters*.py has no solutions.csv row")

    # S10 selection follows the measurements
    results = latest_results()
    for r in solutions:
        hm = HARNESS.search(r["metric_command"])
        if not hm:
            continue
        comp, sel = hm.groups()
        rows = results.get(comp, {})
        if sel not in rows:
            if not waived("S10", f"{r['solution_id']}:unmeasured"):
                bad.append(f"S10 {r['solution_id']}: selected {sel} has no row in results.csv (run with --record)")
            continue
        better = [x["impl"] for x in leaderboard.challengers(rows, sel)]
        if better and not waived("S10", f"{r['solution_id']}:{','.join(sorted(better))}"):
            bad.append(f"S10 {r['solution_id']}: {', '.join(better)} beat the selected {sel} in results.csv; "
                       "adopt the winner or add a waiver naming the pending decision")

    # S11 metric kinds
    sol_ids = {r["solution_id"] for r in solutions}
    for r in components:
        cid, kind = r["component_id"], r["metric_kind"].strip()
        if kind not in METRIC_KINDS:
            bad.append(f"S11 {cid}: metric_kind {kind!r} not in {sorted(METRIC_KINDS)}")
        for sid in patterns(r["solutions"]):
            if sid not in sol_ids:
                bad.append(f"S11 {cid}: solution {sid!r} is not in solutions.csv")
        if r["status"] != "done":
            continue
        if kind == "none":
            bad.append(f"S11 {cid}: status done needs a metric (eval, invariant or exempt: reason)")
        elif kind == "invariant" and r["tests"].startswith("none"):
            bad.append(f"S11 {cid}: invariant metric needs tests that pin it")
        elif kind == "exempt" and not r["metric"].lower().startswith("exempt:"):
            bad.append(f"S11 {cid}: exempt metric must start with 'exempt:' and give the reason")
        elif kind == "eval":
            hm = HARNESS.search(r["metric"])
            if not hm:
                bad.append(f"S11 {cid}: eval metric must be a components/run.py command")
            elif hm.group(2) not in results.get(hm.group(1), {}):
                bad.append(f"S11 {cid}: eval {hm.group(1)}/{hm.group(2)} has no recorded result in results.csv")

    # S8 generated docs
    gen = STRUCT / "COMPONENTS.md"
    if not gen.exists() or gen.read_text() != render_docs(components):
        bad.append("S8 docs/structure/COMPONENTS.md: out of date (run scripts/check_structure.py --write-docs)")

    # S9 waivers still apply
    for w in waivers:
        if (w["rule"], w["path"]) not in used_waivers:
            bad.append(f"S9 {w['rule']} {w['path']}: waiver no longer needed, delete it")
        if not w["reason"].strip():
            bad.append(f"S9 {w['rule']} {w['path']}: waiver has no reason")
    return bad


def main(argv: list[str]) -> int:
    if "--write-docs" in argv:
        (STRUCT / "COMPONENTS.md").write_text(render_docs(read_csv(STRUCT / "components.csv")))
    bad = check()
    for line in bad:
        print(line)
    print(f"structure check: {'FAIL, ' + str(len(bad)) + ' problem(s)' if bad else 'ok'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
