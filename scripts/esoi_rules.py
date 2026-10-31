"""QAR's own rules: S15 (environment variables are documented), Q1 (solution tags come from the maintained list and
link the problems they mitigate) Q2 (every metric's first cascading stage is model-free) and Q3 (no generic code left in QAR). Q1 and the tag lookup live in ``esoi_tags.py`` (the solution tag-solutions); they
are registered and re-exported here, so ``python scripts/esoi_rules.py <id> ...`` still runs the lookup.

It depends on QAR's naming convention (``QAR_*``) and its generated configuration page, and sits beside
``env_vars.py`` (the scanner QAR keeps) in ``scripts/``. It is named in ``esoi.toml`` as a plugin; ``esoi rules``
lists it with esoi's own rules. The top intent and its records are native esoi (``esoi/``), so no rule of
QAR's is needed for them.
"""
import functools
import re
from pathlib import Path

import env_vars
from esoi import cascade
from esoi.rules.base import Context, RuleGroup
from esoi_tags import (TAG_PROCEDURE, main, read_tags, solution_tags, solutions_sharing, split,  # noqa: F401
                       tag_notes, tag_problems, tags_of)


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


def model_free_first_stage(ctx: Context) -> list:
    """Q2: a metric's ``cascading_conditionals`` starts with a stage whose runs call no model (``real=no``), so a result
    exists before any model is paid for. A metric with no such arm yet is waived by metric id, the reason naming the build."""
    bad = []
    for metric in ctx.registry.metrics:
        metric_id = metric.get("metric_id", "")
        stages, errors = cascade.parse(metric.get("cascading_conditionals", ""))
        if errors:
            continue   # S32 reports a list that cannot be read
        if not stages:
            why = "has no cascading_conditionals"
        elif stages[0].real is not False:
            why = f"starts with the {stages[0].name} stage, which is not model-free"
        else:
            continue
        if not ctx.waived("Q2", metric_id):
            bad.append(f"Q2 metrics.csv {metric_id}: {why}; its first stage must be `real=no` "
                       "(e.g. `offline [sample=full | real=no | gate=all-pass | when=...]`) backed by an arm that calls no model: "
                       "the no-model floor, a scripted ideal, a structure check; or waive naming the metric and the build needed")
    return bad


# Q3: words that only Quest-specific code uses. A module with none of them, and no binding to hubofhubs, is generic.
# Visible on purpose: add a word here when a Quest-specific module is flagged by mistake.
QUEST_DOMAIN_WORDS = ("quest", "goal", "habit", "ask", "backend", "questclient", "collection", "decision request",
                      "wisdom", "mentor", "mentorship", "workflow", "autopilot", "milestone", "strategy",
                      "trajectory", "organization", "schedule", "checkin", "check in", "reminder", "notion",
                      "drive", "calendar", "persona", "rep")
HUB_BINDING = "hubofhubs"
DOMAIN_WORD_RX = re.compile(r"\b(%s)(s|es)?\b" % "|".join(w.replace(" ", " ") for w in QUEST_DOMAIN_WORDS))


def words_of(text: str) -> str:
    """Lowercase words of source text: camelCase and punctuation become spaces, so ``QuestClient`` reads ``quest client``."""
    return re.sub(r"[^A-Za-z0-9]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)).lower()


def generic_modules(sources: dict) -> list:
    """The modules (path -> source text) with no Quest-domain word and no import of the hub: generic code that belongs in
    the hub. Empty ``__init__.py`` files are not code."""
    return sorted(path for path, text in sources.items()
                  if text.strip() and HUB_BINDING not in text and not DOMAIN_WORD_RX.search(words_of(text)))


def no_generic_code(ctx: Context) -> list:
    """Q3: every module under the package carries Quest-domain vocabulary or binds to hubofhubs; a module with neither is
    generic and should live in the hub. The waived ones are the hub-migration backlog; their count is printed."""
    root = Path(ctx.root)
    sources = {str(p.relative_to(root)): p.read_text() for p in sorted((root / "quest_ai_runner").rglob("*.py"))}
    flagged = generic_modules(sources)
    bad, waived = [], 0
    for path in flagged:
        if ctx.waived("Q3", path):
            waived += 1
        else:
            bad.append(f"Q3 {path}: no Quest-domain word (see QUEST_DOMAIN_WORDS in scripts/esoi_rules.py) and no hubofhubs "
                       "binding, so it is generic code that belongs in the hub; move it there, or waive it naming why it stays")
    print(f"Q3: {len(sources)} modules scanned, {len(flagged)} generic, {waived} waived (hub-migration backlog)")
    return bad



RULES = [RuleGroup({"S15": "Every environment variable read in the package is a row of docs/configuration.md "
                           "(generated from the code, described in env_descriptions.csv)."}, env_docs),
         RuleGroup({"Q1": "Every solution tag is a row of esoi/tags.csv with a meaning and the problems it mitigates "
                          "(problem_ids that resolve), and every listed tag is used by some solution and not by all."},
                   solution_tags),
         RuleGroup({"Q2": "Every metric's cascading_conditionals starts with a model-free stage (real=no) backed by an arm that "
                          "calls no model (the no-model floor, a scripted ideal, a structure check), or a waiver in "
                          "esoi/waivers.csv names the metric and the build it needs."}, model_free_first_stage),
         RuleGroup({"Q3": "No generic code left in QAR: every module under quest_ai_runner/ contains Quest-domain vocabulary "
                          "(QUEST_DOMAIN_WORDS) or binds to hubofhubs; a module with neither belongs in the hub, or a waiver "
                          "in esoi/waivers.csv lists it as hub-migration backlog."}, no_generic_code)]


if __name__ == "__main__":
    raise SystemExit(main())
