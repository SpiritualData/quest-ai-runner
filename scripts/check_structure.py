#!/usr/bin/env python3
"""Check that the repo still follows the evolvable-structure (ESOI) rules in docs/structure/.

Run from anywhere:  python scripts/check_structure.py [--write-docs] [--strict]
Exit 0 = clean, 1 = violations (printed one per line as ``RULE path: message``).

The registry is data, not code (see docs/structure/README.md, which lists rules S1 to S27 once, in order):
  * components.csv   placement only: component_id, layer, path globs, role, the solutions each serves
  * solutions.csv    each solution: problem, ideal, port, implementations, selected one, metric, gap, action
  * waivers.csv      known, justified exceptions to a rule (each must still apply)
  * COMPONENTS.md    generated from components.csv (--write-docs rewrites it, with configuration.md and tracker/)

Every problem line starts with the id of the rule it breaks (S1 to S27); the one-line statement of each rule is
in docs/structure/README.md, so there is a single list to keep current.
"""
from __future__ import annotations

import ast
import csv
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env_vars  # noqa: E402  (scripts/env_vars.py: the S15 scanner and the configuration.md renderer)

try:                                    # the template reader exists in the full repo; a copied-in fixture lacks it
    import sync_esoi_template  # noqa: E402
except ImportError:                     # pragma: no cover
    sync_esoi_template = None

ROOT = Path(__file__).resolve().parent.parent
STRUCT = ROOT / "docs" / "structure"
TEMPLATE = STRUCT / "esoi_template.csv"
EVALUATION = ROOT / "evaluation" / "components"
MAX_LINES = 1500
PKG = "quest_ai_runner"
# Dependencies point inward: a module may import its own layer or a lower one.
LAYER_RANK = {"core": 0, "adapters": 1, "runner": 2, "config": 3, "cli": 4, "tools": 4}
# Layers a component row may name: the ordered code layers plus the non-code areas of the repo.
COMPONENT_LAYERS = set(LAYER_RANK) | {"prompts", "docs", "evaluation", "examples", "scripts", "tests", "vendor"}
SOLUTION_KINDS = {"eval", "invariant"}
# Folders of evaluation/components that serve every component and are not themselves a solution's metric.
SHARED_EVAL_DIRS = {"__pycache__"}
# Counts of cases are numbers too: "56 offline cases" drifts the day a case is added.
NUMBER_IN_PROSE = re.compile(
    r"(?<![\w-])\d+\s*/\s*\d+(?![\w-])|\$\s?\d|\d+(?:\.\d+)?\s*%|\b\d+\s+of\s+\d+\b"
    r"|\b\d[\d,]*\s+(?:[a-z-]+\s+){0,2}?(?:cases?|scenarios?|queries|questions|tasks|fixtures?|logs|calls|"
    r"documents|sequences|names)\b")
HARNESS = re.compile(r"components/run\.py (\w+) --impl (\w+)")
# A sentence in a gap or action that claims a run is missing, or tells someone to make it (S24).
STALE_CLAIM = re.compile(r"\b(?:not|never) (?:yet )?recorded\b|\bunrecorded\b|\bno recorded\b|^\s*Record\b")


def _load_leaderboard():
    """evaluation/components/leaderboard.py is the one ranking implementation; the check reuses it."""
    import importlib.util
    path = ROOT / "evaluation" / "components" / "leaderboard.py"
    spec = importlib.util.spec_from_file_location("qar_leaderboard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


leaderboard = _load_leaderboard()


def load_records():
    """quest_ai_runner/structure/records.py is the one checker of the intent records (also used by consumers);
    it is standard-library only, so it is loaded by path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qar_structure_records", ROOT / "quest_ai_runner" / "structure" / "records.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


records = load_records()


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
    # Imports under ``if TYPE_CHECKING:`` exist only for annotations and create no runtime dependency.
    typing_only = {id(n) for blk in ast.walk(tree) if isinstance(blk, ast.If)
                   and getattr(blk.test, "id", getattr(blk.test, "attr", "")) == "TYPE_CHECKING"
                   for stmt in blk.body for n in ast.walk(stmt)}
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
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


def uses_environ(path: Path) -> list[int]:
    """Lines that read the process environment (os.environ, os.getenv, or those names imported from os)."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv") \
                and isinstance(node.value, ast.Name) and node.value.id == "os":
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "os" \
                and any(a.name in ("environ", "getenv", "putenv") for a in node.names):
            lines.append(node.lineno)
    return lines


def render_docs(components: list[dict]) -> str:
    lines = [
        "# Components",
        "",
        "Generated from `docs/structure/components.csv` by `python scripts/check_structure.py --write-docs`.",
        "Edit the CSV, never this file. The rules are the numbered list in `docs/structure/README.md`.",
        "",
        "| Component | Layer | Role | Serves |",
        "|---|---|---|---|",
    ]
    for r in components:
        cell = lambda k: r[k].replace("|", "/").replace(";", ", ")
        lines.append(f"| `{r['component_id']}` | {r['layer']} | {cell('role')} | {cell('solutions')} |")
    return "\n".join(lines) + "\n"


ALTERNATIVE_MARKERS = ("ALTERNATIVE", "BASELINE", "ABLATION", "FLOOR", "VALIDITY")


def impl_kind(path: Path) -> str:
    """Classify an evaluation implementation file: ``alternative`` (declares itself a baseline or
    alternative), ``live`` (imports or drives quest_ai_runner code) or ``unclear`` (neither)."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, OSError):
        return "unclear"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ALTERNATIVE" for t in node.targets):
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                return "alternative"
    doc = (ast.get_docstring(tree) or "").lstrip().upper()
    if any(doc.startswith(m) for m in ALTERNATIVE_MARKERS):
        return "alternative"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == PKG:
            return "live"
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == PKG for a in node.names):
            return "live"
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "common"
                and node.attr == "build_offline_orchestrator"):
            return "live"   # drives the real Orchestrator through the shared offline builder
    return "unclear"


def load_selection_table() -> dict:
    """core/selection_table.py is pure data, so it is loaded on its own (no package import)."""
    import importlib.util
    path = ROOT / PKG / "core" / "selection_table.py"
    spec = importlib.util.spec_from_file_location("qar_selection_table", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SELECTION


def join_metrics(solutions: list[dict], metrics: list[dict]) -> list[dict]:
    """Give each solution row the `metric` (names) and `metric_command` (commands) of its metric records.

    A solution links its metrics through `metric_ids`; the name and command live on the metric record
    (metrics.csv), so the rules that read a solution's metric_command see the one source. A registry with no
    metrics.csv keeps the columns it has.
    """
    if not metrics:
        return solutions
    by_id = {m["metric_id"]: m for m in metrics}
    for row in solutions:
        linked = [by_id[i] for i in records.ids(row.get("metric_ids", "")) if i in by_id]
        row["metric"] = "; ".join(m["name"] for m in linked if m["name"])
        row["metric_command"] = "; ".join(m["command"] for m in linked if m["command"])
    return solutions


def check() -> list[str]:
    bad: list[str] = []
    components = read_csv(STRUCT / "components.csv")
    solutions = join_metrics(read_csv(STRUCT / "solutions.csv"), read_csv(STRUCT / "metrics.csv"))
    waivers = read_csv(STRUCT / "waivers.csv")
    files = repo_files()
    used_waivers: set[tuple[str, str]] = set()
    try:
        table = load_selection_table()
    except Exception as e:  # noqa: BLE001
        table = {}
        bad.append(f"S14 {PKG}/core/selection_table.py: cannot load ({type(e).__name__}: {e})")

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
        if r["layer"] not in COMPONENT_LAYERS:
            bad.append(f"S2 {cid}: layer {r['layer']!r} not in {sorted(COMPONENT_LAYERS)}")
        if not r["role"].strip():
            bad.append(f"S2 {cid}: column role is empty (state the role the component owns)")
        for p in patterns(r["path"]):
            rx = glob_re(p)
            hit = [f for f in files if rx.match(f)]
            if not hit:
                bad.append(f"S1 {cid}: path pattern {p!r} matches no file")
            for f in hit:
                if cid not in owners[f]:
                    owners[f].append(cid)
    for f, ids in owners.items():
        if not ids:
            bad.append(f"S1 {f}: belongs to no component (add it to docs/structure/components.csv)")
        elif len(ids) > 1:
            bad.append(f"S1 {f}: belongs to several components {ids}")

    # S3 root
    for f in files:
        if "/" not in f and f.endswith(".py"):
            bad.append(f"S3 {f}: .py file at the repo root (use tests/, scripts/ or the package)")

    # layer of a file = the layer column of the component that owns it (S6)
    # (a file inside core/, adapters/, runner/ or tools/ takes its package, so a file placed in the
    # wrong component cannot hide a violation)
    comp_layer = {r["component_id"]: r["layer"] for r in components}
    file_layer = {}
    for f, ids in owners.items():
        pkg = f.split("/")[1] if f.startswith(PKG + "/") and f.count("/") > 1 else ""
        if pkg in LAYER_RANK:
            file_layer[f] = pkg
        elif ids:
            file_layer[f] = comp_layer[ids[0]]

    def top_layer(top: str) -> str:
        """Layer of ``quest_ai_runner/<top>`` (a module or a package), from its owning component."""
        for cand in (f"{PKG}/{top}.py", f"{PKG}/{top}/__init__.py"):
            if cand in file_layer:
                return file_layer[cand]
        return next((v for f, v in file_layer.items() if f.startswith(f"{PKG}/{top}/")), "")

    # S4 size, S5 docstring, S6 direction, S13 config
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
            layer = file_layer.get(f, "")
            for line, top in module_imports(path, dotted):
                target = top_layer(top)
                if layer in LAYER_RANK and target in LAYER_RANK and LAYER_RANK[target] > LAYER_RANK[layer] \
                        and not waived("S6", f"{f}:{top}"):
                    bad.append(f"S6 {f}:{line}: {layer} imports {top} ({target}); dependencies point inward")
            if f.startswith(PKG + "/core/") and uses_environ(path) and not waived("S13", f):
                bad.append(f"S13 {f}:{uses_environ(path)[0]}: core reads the environment; take the value from "
                           "RunnerConfig (or add a waiver 'migrate into RunnerConfig')")

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
        if sid in table:
            names = table[sid].get("implementations", {})
            if r["selected"] and r["selected"] not in names:
                bad.append(f"S7 {sid}: selected {r['selected']!r} is not an implementation name in the selection "
                           f"table (choices: {', '.join(sorted(names))})")
        elif r["selected"] and r["selected"] not in impls:
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
        bad.append(f"S7 {p}: port is listed in the `port` cell of no solutions.csv row (add it to the row of the "
                   "solution it serves; a port needs no row of its own)")

    # S12 evaluation implementations measure live code (or say they are alternatives)
    kinds: dict[str, str] = {}
    for impl_path in sorted((ROOT / "evaluation" / "components").glob("*/impls/*.py")):
        if impl_path.name == "__init__.py":
            continue
        rel = str(impl_path.relative_to(ROOT))
        kinds[rel] = impl_kind(impl_path)
        if kinds[rel] == "unclear" and not waived("S12", rel):
            bad.append(f"S12 {rel}: neither wraps a live {PKG} class or function nor declares itself an "
                       "alternative (set ALTERNATIVE = True, or open the docstring with ALTERNATIVE/BASELINE/"
                       "ABLATION/FLOOR/VALIDITY): the number would not measure the live code")
    for r in solutions:
        sel = r["selected"].split("::")[0]
        if sel in kinds and kinds[sel] == "alternative" and not waived("S12", f"{r['solution_id']}:{Path(sel).stem}"):
            bad.append(f"S12 {r['solution_id']}: selected {sel} is an alternative, not live code; select the live "
                       "implementation or add a waiver naming the decision")
        hm = HARNESS.search(r["metric_command"])
        if hm:
            measured = f"evaluation/components/{hm.group(1)}/impls/{hm.group(2)}.py"
            if kinds.get(measured) == "alternative" and not waived("S12", f"{r['solution_id']}:{hm.group(2)}"):
                bad.append(f"S12 {r['solution_id']}: the measured implementation {measured} is an alternative, not "
                           "live code, so the recorded number would not be the selected one's")

    # S14 selection table: switching an implementation is data
    port_rows = {r["solution_id"]: r for r in solutions if r["port"].strip()}
    for sid in sorted(set(port_rows) - set(table)):
        bad.append(f"S14 {sid}: solutions.csv row has port {port_rows[sid]['port']} but no entry in "
                   f"{PKG}/core/selection_table.py (add one so resolve({sid!r}) works)")
    for sid in sorted(set(table) - set(port_rows)):
        bad.append(f"S14 {sid}: selection table entry has no solutions.csv row with a port")
    for sid, entry in table.items():
        impls = entry.get("implementations", {})
        if entry.get("default") not in impls:
            bad.append(f"S14 {sid}: default {entry.get('default')!r} is not among its implementations")
        if sid in port_rows and port_rows[sid]["selected"] != entry.get("default"):
            bad.append(f"S23 {sid}: selected {port_rows[sid]['selected']!r} differs from the selection table default "
                       f"{entry.get('default')!r}; the registry, the table and the live default must name one "
                       "implementation (change both together)")
        listed = {i.split("::")[0] for i in patterns(port_rows[sid]["implementations"])} if sid in port_rows else set()
        for name, ref in impls.items():
            target = ref["target"] if isinstance(ref, dict) else ref
            module = target.partition(":")[0]
            mod_path = module.replace(".", "/") + ".py"
            if not (ROOT / mod_path).exists():
                bad.append(f"S14 {sid}: implementation {name!r} points at {module}, which does not exist")
            elif sid in port_rows and mod_path not in listed:
                bad.append(f"S14 {sid}: implementation {name!r} lives in {mod_path}, which solutions.csv does not list")

    # S16 provenance: results without a cases_sha are unverified
    for comp, rows in sorted(latest_results().items()):
        for impl, row in sorted(rows.items()):
            if leaderboard.unverified(row) and not waived("S16", f"{comp}/{impl}"):
                bad.append(f"S16 {comp}/{impl}: latest results.csv row has no cases_sha (re-run with --record, "
                           "or add a waiver saying why it cannot be re-run)")

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
        invariant = r.get("metric_kind", "").strip() == "invariant"
        better = [] if invariant else [x["impl"] for x in leaderboard.challengers(rows, sel)]
        if better and not waived("S10", f"{r['solution_id']}:{','.join(sorted(better))}"):
            bad.append(f"S10 {r['solution_id']}: {', '.join(better)} beat the selected {sel} in results.csv; "
                       "adopt the winner or add a waiver naming the pending decision")

    # S24 recorded claims are true: every arm a metric_command names has a row, and a gap or action that says an
    # implementation is not recorded (or tells someone to record it) names none that already has a row
    for r in solutions:
        sid = r["solution_id"]
        for comp, impl in HARNESS.findall(r["metric_command"])[1:]:   # the first arm is S10's
            if impl not in results.get(comp, {}) and not waived("S24", f"{sid}:{impl}"):
                bad.append(f"S24 {sid}: the metric_command names {comp}/{impl}, which has no row in results.csv "
                           "(record it, or name the recorded arm and say in the gap that the other is unrecorded)")
        recorded = set()
        for i in patterns(r["implementations"]):
            im = re.match(r"evaluation/components/(\w+)/impls/(\w+)\.py$", i.split("::")[0])
            if im and im.group(2) in results.get(im.group(1), {}):
                recorded.add(im.group(2))
        for sentence in re.split(r"(?<=[.;])\s+", r["gap"] + " " + r["action"]):
            if not STALE_CLAIM.search(sentence):
                continue
            for stem in sorted(recorded):
                if re.search(rf"\b{re.escape(stem)}\b", sentence) and not waived("S24", f"{sid}:{stem}"):
                    bad.append(f"S24 {sid}: the gap or action says {stem} is not recorded, but results.csv has a row "
                               f"for it ({sentence.strip()[:70]!r}); update the text")

    # S25 an invariant solution's cases are proved to discriminate: its evaluation folder has a broken/ baseline module
    for r in solutions:
        sid = r["solution_id"]
        hm = HARNESS.search(r["metric_command"])
        if r.get("metric_kind", "").strip() != "invariant" or not hm:
            continue
        broken = EVALUATION / hm.group(1) / "broken"
        baselines = [f for f in broken.glob("*.py") if f.name != "__init__.py"] if broken.is_dir() else []
        if not baselines and not waived("S25", sid):
            bad.append(f"S25 {sid}: evaluation/components/{hm.group(1)}/broken/ has no baseline module (add the naive "
                       "implementation a first draft would write; a test proves the cases fail it)")

    # S11 metric kinds
    sol_ids ={r["solution_id"] for r in solutions}
    for r in solutions:
        sid, kind = r["solution_id"], r.get("metric_kind", "").strip()
        if kind not in SOLUTION_KINDS:
            bad.append(f"S11 {sid}: solution metric_kind {kind!r} not in {sorted(SOLUTION_KINDS)}")
        elif kind == "invariant" and not r.get("invariant_reason", "").strip():
            bad.append(f"S11 {sid}: an invariant solution states in invariant_reason why 'best' cannot vary")
        elif kind == "eval" and r.get("invariant_reason", "").strip():
            bad.append(f"S11 {sid}: invariant_reason is set but metric_kind is eval")
    for r in components:
        for sid in patterns(r["solutions"]):
            if sid not in sol_ids:
                bad.append(f"S11 {r['component_id']}: solution {sid!r} is not in solutions.csv")

    # S11 reverse link: every solution is owned by some component
    linked = {sid for r in components for sid in patterns(r["solutions"])}
    for r in solutions:
        sid = r["solution_id"]
        if sid not in linked and not waived("S11", f"solution:{sid}"):
            bad.append(f"S11 {sid}: no component lists it in `solutions` (link it in components.csv or add a waiver)")

    # S8 generated docs
    gen = STRUCT / "COMPONENTS.md"
    if not gen.exists() or gen.read_text() != render_docs(components):
        bad.append("S8 docs/structure/COMPONENTS.md: out of date (run scripts/check_structure.py --write-docs)")

    # S15 environment variables are documented
    doc_path = ROOT / "docs" / "configuration.md"
    doc_text = doc_path.read_text() if doc_path.exists() else ""
    described = env_vars.descriptions(ROOT)
    for var, where in env_vars.env_vars_in_code(ROOT).items():
        if waived("S15", var):
            continue
        if f"`{var}`" not in doc_text:
            bad.append(f"S15 {var}: read in {where[0]} but missing from docs/configuration.md "
                       "(run scripts/check_structure.py --write-docs)")
        elif var not in described and "| - |" in next(
                (ln for ln in doc_text.splitlines() if ln.startswith(f"| `{var}` |")), ""):
            bad.append(f"S15 {var}: read in {where[0]} but explained nowhere; add a line to "
                       "docs/structure/env_descriptions.csv")
    if env_vars.without_explained_in(doc_text) != env_vars.without_explained_in(env_vars.render_env_doc(ROOT)):
        bad.append("S15 docs/configuration.md: out of date (run scripts/check_structure.py --write-docs)")

    # S18 ids are unique: a tracker row keyed twice would silently merge two things
    from collections import Counter
    for sid, n in Counter(s["solution_id"] for s in read_csv(STRUCT / "solutions.csv")).items():
        if n > 1:
            bad.append(f"S18 solutions.csv: solution id {sid} appears {n} times")
    for cid, n in Counter(c["component_id"] for c in components).items():
        if n > 1:
            bad.append(f"S18 components.csv: component id {cid} appears {n} times")

    for r in components:
        for col in ("solutions", "path"):
            for ident, n in Counter(patterns(r[col])).items():
                if n > 1:
                    bad.append(f"S18 components.csv {r['component_id']}: `{col}` lists {ident} {n} times")
    for r in solutions:
        for col in ("port", "implementations"):
            for ident, n in Counter(patterns(r[col])).items():
                if n > 1:
                    bad.append(f"S18 solutions.csv {r['solution_id']}: `{col}` lists {ident} {n} times")

    # S17 tracker tabs match the registry (tracking_* columns are free)
    import importlib.util
    tracker_spec = importlib.util.spec_from_file_location("qar_build_tracker", ROOT / "scripts" / "build_tracker.py")
    tracker = importlib.util.module_from_spec(tracker_spec)
    tracker_spec.loader.exec_module(tracker)
    for tab, rows in tracker.build().items():
        key = tracker.COLUMNS[tab][0]
        for ident, n in Counter(r[key] for r in rows).items():
            if n > 1:
                bad.append(f"S18 tracker {tab}: id {ident} appears {n} times (two implementations share a file name)")
    for tab in tracker.stale():
        bad.append(f"S17 docs/structure/tracker/{tab}.csv: out of date (run scripts/build_tracker.py)")

    # S21 every evaluation folder is a solution's metric (or shared infrastructure)
    metric_text = " ".join(r["metric_command"] + " " + r["metric"] for r in solutions)
    for folder in sorted(p for p in (ROOT / "evaluation" / "components").iterdir() if p.is_dir()):
        name = folder.name
        if name in SHARED_EVAL_DIRS:
            continue
        named = re.search(rf"run\.py {name}\b", metric_text) or re.search(rf"evaluation/components/{name}\b", metric_text)
        if not named:
            bad.append(f"S21 evaluation/components/{name}: no solutions.csv metric_command or declared arm names this "
                       "folder; make it a solution's metric or delete it")

    # S22 numbers live in results.csv only
    prose = [("solutions.csv", r["solution_id"], col, r[col]) for r in solutions for col in ("gap", "action")]
    prose += [("waivers.csv", f"{w['rule']} {w['path']}", "reason", w["reason"]) for w in waivers]
    for where, ident, col, text in prose:
        hit = NUMBER_IN_PROSE.search(text)
        if hit:
            bad.append(f"S22 {where} {ident}: `{col}` states a number ({hit.group(0)!r}); numbers live in results.csv "
                       "and the leaderboard, so name what was measured instead")

    # S19 every component serves a solution; S20 every solution is complete and has a current result
    for r in components:
        if not patterns(r["solutions"]):
            bad.append(f"S19 {r['component_id']}: serves no solution (name one in the `solutions` column, "
                       "or remove the code: nothing in this repo exists outside a solution)")
    strict = "--strict" in sys.argv
    for r in solutions:
        sid = r["solution_id"]
        needed = ("selected",) if waived("S26", sid) else ("problem_ids", "ideal_ids", "metric_ids", "selected")
        for col in needed:
            if col in r and not r[col].strip():
                bad.append(f"S20 {sid}: `{col}` is empty (a solution needs a problem, an ideal, a metric and a selected implementation)")
        if not patterns(r["implementations"]):
            bad.append(f"S20 {sid}: lists no implementation")
        hm = HARNESS.search(r["metric_command"])
        if not hm:
            if not waived("S20", sid):
                bad.append(f"S20 {sid}: metric_command is not an evaluation/components/run.py command "
                           "(a metric is a recorded run, not tests alone)")
            continue
        comp, sel = hm.groups()
        row = results.get(comp, {}).get(sel)
        if row is None:
            continue  # S10 reports the missing row
        now = leaderboard.cases_digest(comp)
        if not row.get("git_sha", "").strip():
            why = "its latest row has no git_sha"
        elif row.get("cases_sha", "") != now:
            why = f"its latest row was recorded on other cases (cases_sha {row.get('cases_sha') or 'blank'}, now {now})"
        elif strict and row["git_sha"].endswith("-dirty"):
            why = "its latest row was recorded on uncommitted code"
        else:
            continue
        if not waived("S20", sid):
            bad.append(f"S20 {sid}: {why}; re-run `run.py {comp} --impl {sel} --record` on committed code")

    # S26 the intent records specify every field of the evolvable-structure template, and link as it says;
    # S27 the parsed template is current with the skill (skipped where the skill is not reachable)
    if TEMPLATE.exists():
        template = records.load_template(TEMPLATE)
        pending = {r["solution_id"] for r in solutions if waived("S26", r["solution_id"])}
        bad += [f"S26 {line}" for line in records.check_records(STRUCT, template, pending)]
        record_files = (("problems.csv", "problem_id", "problem"), ("ideals.csv", "ideal_id", "ideal"),
                        ("solutions.csv", "solution_id", "solution"), ("implementations.csv", "implementation_id", "implementation"),
                        ("metrics.csv", "metric_id", "metric"))
        prose = []
        for name, key, entity in record_files:
            columns = [t["field_key"] for t in template.get(entity, []) if t["group"] != "meta"]
            rows = solutions if entity == "solution" else read_csv(STRUCT / name)
            prose += [(name, row[key], col, row[col]) for row in rows for col in columns if col in row]
        for name, ident, col, text in prose:
            hit = NUMBER_IN_PROSE.search(text)
            if hit:
                bad.append(f"S22 {name} {ident}: `{col}` states a number ({hit.group(0)!r}); numbers live in results.csv")
        skill = sync_esoi_template.skill_path() if sync_esoi_template else None
        if skill is not None and skill.exists():
            rows, _ = sync_esoi_template.parse(skill.read_text())
            if sync_esoi_template.render_csv(rows) != TEMPLATE.read_text():
                bad.append("S27 docs/structure/esoi_template.csv: stale against the evolvable-structure skill "
                           "(run scripts/sync_esoi_template.py, then fill the columns it adds)")

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
        (ROOT / "docs" / "configuration.md").write_text(env_vars.render_env_doc(ROOT))
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_tracker.py")], check=True,
                       stdout=subprocess.DEVNULL)
    bad = check()
    for line in bad:
        print(line)
    print(f"structure check: {'FAIL, ' + str(len(bad)) + ' problem(s)' if bad else 'ok'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
