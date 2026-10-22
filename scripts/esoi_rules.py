"""QAR's own rules: S15 (environment variables are documented) and Q1 (solution tags come from the maintained list).

It depends on QAR's naming convention (``QAR_*``) and its generated configuration page, and sits beside
``env_vars.py`` (the scanner QAR keeps) in ``scripts/``. It is named in ``esoi.toml`` as a plugin; ``esoi rules``
lists it with esoi's own rules. The top intent and its records are native esoi (``esoi/``), so no rule of
QAR's is needed for them.
"""
import csv
import functools
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
            bad.append(f"S15 {var}: read in {where} but explained nowhere; add a line to esoi/env_descriptions.csv")
    if result["stale"]:
        bad.append("S15 docs/configuration.md: out of date (run `esoi check --write-docs`)")
    return bad


def write_docs(ctx: Context) -> None:
    (Path(ctx.root) / "docs" / "configuration.md").write_text(env_vars.render_env_doc(Path(ctx.root)))


TAG_PROCEDURE = "see docs/spec-selection.md, Solution tags: a tag names a risk and exists only with its back-fill"


def tag_problems(listed: list, solutions: list) -> list:
    """Q1 problem lines for the tag list ``listed`` (rows with tag, meaning, risk) and the solutions' tags columns.

    What can be checked without judgement: every tag a solution uses is listed, every listed tag states its meaning
    and its risk, and every listed tag is used by at least two solutions and by fewer than all of them."""
    known = {row.get("tag", "").strip(): row for row in listed if row.get("tag", "").strip()}
    users = {tag: [] for tag in known}
    bad = []
    for solution in solutions:
        sid = solution.get("solution_id", "")
        for tag in [t.strip() for t in (solution.get("tags") or "").split(";") if t.strip()]:
            if tag in users:
                users[tag].append(sid)
            else:
                bad.append(f"Q1 {sid}: tag `{tag}` is not in esoi/tags.csv ({TAG_PROCEDURE})")
    for tag, row in known.items():
        for column in ("meaning", "risk"):
            if not (row.get(column) or "").strip():
                bad.append(f"Q1 esoi/tags.csv {tag}: no {column} stated ({TAG_PROCEDURE})")
        count = len(users[tag])
        if count == 0:
            bad.append(f"Q1 esoi/tags.csv {tag}: used by no solution; retire it ({TAG_PROCEDURE})")
        elif count == 1:
            bad.append(f"Q1 esoi/tags.csv {tag}: used by one solution only ({users[tag][0]}); back-fill it or retire it "
                       f"({TAG_PROCEDURE})")
        elif solutions and count == len(solutions):
            bad.append(f"Q1 esoi/tags.csv {tag}: on every solution, so it identifies nothing; retire it ({TAG_PROCEDURE})")
    return bad


def solution_tags(ctx: Context) -> list:
    path = Path(ctx.root) / ctx.config.structure_dir / "tags.csv"
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        listed = list(csv.DictReader(handle))
    return tag_problems(listed, ctx.every_solution())


RULES = [RuleGroup({"S15": "Every environment variable read in the package is a row of docs/configuration.md "
                           "(generated from the code, described in env_descriptions.csv)."}, env_docs),
         RuleGroup({"Q1": "Every solution tag is a row of esoi/tags.csv with a meaning and a risk, and every listed tag is "
                          "used by at least two solutions and not by all."}, solution_tags)]
