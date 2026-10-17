"""BASELINE: a checker that only does file coverage (every file belongs to exactly one component, every
glob matches a file) and, for a consumer registry, only that the required columns exist. The naive
alternative the full check is measured against. Zero model calls."""
import csv
import re
import subprocess

from check_the_structure import fixture


def glob_re(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def coverage(root):
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                           capture_output=True, text=True).stdout.split()
    files = [f for f in files if (root / f).is_file()]
    rows = fixture.read_rows(root / "docs/structure/components.csv")
    owners = {f: [] for f in files}
    problems = []
    for r in rows:
        for pat in filter(None, (p.strip() for p in r["path"].split(";"))):
            hit = [f for f in files if glob_re(pat).match(f)]
            if not hit:
                problems.append(f"S1 {r['component_id']}: path pattern {pat!r} matches no file")
            for f in hit:
                owners[f].append(r["component_id"])
    problems += [f"S1 {f}: belongs to no component" for f, o in owners.items() if not o]
    problems += [f"S1 {f}: belongs to several components" for f, o in owners.items() if len(set(o)) > 1]
    return problems


def build(meter):  # noqa: ARG001
    def check(kind, args):
        root = fixture.fresh_repo(kind, args.get("edits", []))
        if kind == "audit":
            problems = []  # a coverage checker has no per-component verdicts
        elif kind == "repo":
            problems = coverage(root)
        else:
            with (root / "registry.csv").open(newline="") as fh:
                have = csv.DictReader(fh).fieldnames or []
            from quest_ai_runner.structure.consumer_check import REQUIRED_COLUMNS
            problems = [f"C1 registry: missing {c}" for c in REQUIRED_COLUMNS if c not in have]
        return {"exit": 1 if problems else 0, "problems": problems}
    return check
