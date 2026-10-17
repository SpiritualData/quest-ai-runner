"""QAR's own rules: S15 (environment variables are documented) and S26 for QAR's overall network.

These two stay in quest-ai-runner. S15 depends on QAR's naming convention (``QAR_*``) and its generated
configuration page. The S26 part checks the `overall_problem_id`, `overall_ideal_id` and `area` columns against
``qar_solution.md``, QAR's overall network written as markdown; QAR can drop it by moving that network into the
record format esoi checks (an ``overall`` folder and ``parent_*`` columns, see docs/migration/README.md). It sits beside ``env_vars.py`` (the scanner QAR keeps) in
``scripts/`` and is named in ``esoi.toml`` as a plugin.
"""
import functools
import re
from pathlib import Path

import env_vars
from esoi.rules.base import Context, RuleGroup


# render_env_doc scans the package again; scan it once per run.
env_vars.env_vars_in_code = functools.lru_cache(maxsize=2)(env_vars.env_vars_in_code)


def scan_env_docs(root: Path) -> dict:
    """The expensive part: every variable the package reads, and whether the configuration page is current."""
    doc_path = root / "docs" / "configuration.md"
    doc_text = doc_path.read_text() if doc_path.exists() else ""
    described = env_vars.descriptions(root)
    found = env_vars.env_vars_in_code(root)
    missing, unexplained = [], []
    for var, where in found.items():
        if f"`{var}`" not in doc_text:
            missing.append([var, where[0]])
        elif var not in described and "| - |" in next(
                (ln for ln in doc_text.splitlines() if ln.startswith(f"| `{var}` |")), ""):
            unexplained.append([var, where[0]])
    stale = env_vars.without_explained_in(doc_text) != env_vars.without_explained_in(env_vars.render_env_doc(root))
    return {"vars": list(found), "missing": missing, "unexplained": unexplained, "stale": stale}


# What scan_env_docs reads: the package, the docs and the files that explain a variable.
INPUTS = ["quest_ai_runner/**", "docs/**", "README.md", ".env.example", "CLAUDE.md", "scripts/env_vars.py"]


def env_docs(ctx: Context) -> list:
    root = Path(ctx.root)
    result = ctx.memo("qar-env-docs", INPUTS, lambda: scan_env_docs(root))
    waived = {var for var in result["vars"] if ctx.waived("S15", var)}
    bad = []
    for var, where in result["missing"]:
        if var not in waived:
            bad.append(f"S15 {var}: read in {where} but missing from docs/configuration.md (run `esoi check --write-docs`)")
    for var, where in result["unexplained"]:
        if var not in waived:
            bad.append(f"S15 {var}: read in {where} but explained nowhere; add a line to docs/structure/env_descriptions.csv")
    if result["stale"]:
        bad.append("S15 docs/configuration.md: out of date (run `esoi check --write-docs`)")
    return bad


def overall_markdown(ctx: Context) -> list:
    """Each detailed record names the QP/QI item of qar_solution.md it serves, and each solution names an area."""
    overall = Path(ctx.root) / "docs" / "structure" / "qar_solution.md"
    if not overall.exists():
        return []
    bad = []
    text = overall.read_text()
    pending = {r["solution_id"] for r in ctx.solutions if ctx.waived("S26", r["solution_id"])}
    for rows, key, col, prefix in ((ctx.registry.problems, "problem_id", "overall_problem_id", "QP"),
                                   (ctx.registry.ideals, "ideal_id", "overall_ideal_id", "QI")):
        for row in rows:
            if not row.get(col) or not re.search(rf"^### {re.escape(row[col])} ", text, re.M):
                bad.append(f"S26 {row[key]}: `{col}` {row.get(col)!r} is not a `### {prefix}n` item of qar_solution.md")
    areas = set(re.findall(r"^\d+\. \*\*([^*]+):\*\*", text, re.M))
    for row in ctx.solutions:
        if row["solution_id"] not in pending and row.get("area", "").split(" ", 1)[-1] not in areas:
            bad.append(f"S26 {row['solution_id']}: `area` {row.get('area')!r} is not an area of qar_solution.md")
    return bad


def write_docs(ctx: Context) -> None:
    (Path(ctx.root) / "docs" / "configuration.md").write_text(env_vars.render_env_doc(Path(ctx.root)))


RULES = [RuleGroup({"S26": "Detailed records name the item and area of qar_solution.md they serve."}, overall_markdown),
         RuleGroup({"S15": "Every environment variable read in the package is a row of docs/configuration.md "
                           "(generated from the code, described in env_descriptions.csv)."}, env_docs)]
