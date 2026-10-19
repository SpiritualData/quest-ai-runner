"""QAR's own rule: S15 (environment variables are documented), which stays in quest-ai-runner.

It depends on QAR's naming convention (``QAR_*``) and its generated configuration page, and sits beside
``env_vars.py`` (the scanner QAR keeps) in ``scripts/``. It is named in ``esoi.toml`` as a plugin; ``esoi rules``
lists it with esoi's own rules. The top intent and its records are native esoi (``esoi/``), so no rule of
QAR's is needed for them.
"""
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


RULES = [RuleGroup({"S15": "Every environment variable read in the package is a row of docs/configuration.md "
                           "(generated from the code, described in env_descriptions.csv)."}, env_docs)]
