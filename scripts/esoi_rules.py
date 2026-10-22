"""QAR's own rules: S15 (environment variables are documented) and Q1 (solution tags come from the maintained list and
link the problems they mitigate), plus the tag lookup (``tags_of``, ``solutions_sharing``, and ``main`` as a command).

It depends on QAR's naming convention (``QAR_*``) and its generated configuration page, and sits beside
``env_vars.py`` (the scanner QAR keeps) in ``scripts/``. It is named in ``esoi.toml`` as a plugin; ``esoi rules``
lists it with esoi's own rules. The top intent and its records are native esoi (``esoi/``), so no rule of
QAR's is needed for them.
"""
import csv
import functools
import sys
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


TAG_PROCEDURE = "see docs/spec-selection.md, Solution tags: a tag links the problems it mitigates and is back-filled"


def split(cell) -> list:
    return [part.strip() for part in (cell or "").split(";") if part.strip()]


def tags_of(solution: dict) -> list:
    """A solution's tags as the lookup reads them: its ``tags`` column plus its connected problems (``problem_ids``),
    which count as tags too (the evolvable-structure skill, Solution, Tags) but are not sufficient on their own, so
    they are never copied into the column."""
    return list(dict.fromkeys(split(solution.get("tags")) + split(solution.get("problem_ids"))))


def solutions_sharing(wanted, solutions: list) -> list:
    """The ids of the solutions that carry any of ``wanted`` (tag ids or problem ids), in record order."""
    wanted = set(wanted)
    return [s.get("solution_id", "") for s in solutions if wanted & set(tags_of(s))]


def tag_problems(listed: list, solutions: list, problem_ids=None) -> list:
    """Q1 problem lines for the tag list ``listed`` (rows with tag, meaning, problem_ids) and the solutions' tags.

    What can be checked without judgement: every tag a solution uses is listed; every listed tag states its meaning
    and links at least one problem, and each linked problem exists (``problem_ids``, the ids of every problem record;
    None skips that check); every listed tag is used by some solution and not by all of them (a tag on every
    solution classifies nothing)."""
    known = {row.get("tag", "").strip(): row for row in listed if row.get("tag", "").strip()}
    users = {tag: [] for tag in known}
    bad = []
    for solution in solutions:
        sid = solution.get("solution_id", "")
        for tag in split(solution.get("tags")):
            if tag in users:
                users[tag].append(sid)
            else:
                bad.append(f"Q1 {sid}: tag `{tag}` is not in esoi/tags.csv ({TAG_PROCEDURE})")
    for tag, row in known.items():
        if not (row.get("meaning") or "").strip():
            bad.append(f"Q1 esoi/tags.csv {tag}: no meaning stated ({TAG_PROCEDURE})")
        linked = split(row.get("problem_ids"))
        if not linked:
            bad.append(f"Q1 esoi/tags.csv {tag}: links no problem, so it names no risk it mitigates ({TAG_PROCEDURE})")
        for pid in linked:
            if problem_ids is not None and pid not in problem_ids:
                bad.append(f"Q1 esoi/tags.csv {tag}: linked problem `{pid}` is not a problem record ({TAG_PROCEDURE})")
        count = len(users[tag])
        if count == 0:
            bad.append(f"Q1 esoi/tags.csv {tag}: used by no solution; retire it ({TAG_PROCEDURE})")
        elif solutions and count == len(solutions):
            bad.append(f"Q1 esoi/tags.csv {tag}: on every solution, so it identifies nothing; retire it ({TAG_PROCEDURE})")
    return bad


def tag_notes(listed: list, solutions: list) -> list:
    """Warnings, not failures: a tag on one solution only. It still lets the author of a second solution find the
    first (R1, R2), so it is allowed, but it is worth a back-fill check."""
    out = []
    for row in listed:
        tag = (row.get("tag") or "").strip()
        users = [s.get("solution_id", "") for s in solutions if tag in split(s.get("tags"))]
        if tag and len(users) == 1:
            out.append(f"note: Q1 esoi/tags.csv {tag}: used by one solution only ({users[0]}); check the back-fill "
                       f"({TAG_PROCEDURE})")
    return out


def read_tags(root: Path, structure_dir) -> list:
    path = Path(root) / structure_dir / "tags.csv"
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def solution_tags(ctx: Context) -> list:
    listed = read_tags(ctx.root, ctx.config.structure_dir)
    if not listed:
        return []
    solutions = ctx.every_solution()
    for line in tag_notes(listed, solutions):
        print(line, file=sys.stderr)
    known = {row.get("problem_id", "") for row in ctx.registry.problems}
    return tag_problems(listed, solutions, known)


RULES = [RuleGroup({"S15": "Every environment variable read in the package is a row of docs/configuration.md "
                           "(generated from the code, described in env_descriptions.csv)."}, env_docs),
         RuleGroup({"Q1": "Every solution tag is a row of esoi/tags.csv with a meaning and the problems it mitigates "
                          "(problem_ids that resolve), and every listed tag is used by some solution and not by all."},
                   solution_tags)]


def main(argv=None) -> int:
    """``python scripts/esoi_rules.py <tag, problem or solution id> ...``: the solutions that carry any of them, through
    their tags column or their connected problems. A solution id stands for its own tags."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("ids", nargs="+")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    with (root / "esoi" / "solutions.csv").open(newline="") as handle:
        solutions = list(csv.DictReader(handle))
    by_id = {s.get("solution_id", ""): s for s in solutions}
    wanted = [t for i in args.ids for t in (tags_of(by_id[i]) if i in by_id else [i])]
    for sid in solutions_sharing(wanted, solutions):
        print(f"{sid}: {';'.join(t for t in tags_of(by_id[sid]) if t in wanted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
