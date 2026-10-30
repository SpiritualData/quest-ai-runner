#!/usr/bin/env python3
"""Builds the case set of the lean-launch experiment (README.md): 12 real small and medium commits of this repository, each
turned into a scratch copy whose source is put back to the state before the commit and whose tests are the commit's own.

NOT RUN by anything in the repository, and it never runs a model: it reads git history, copies files and writes a JSON file. By
default it is a dry run that only lists the commits it would use. ``--build`` makes the scratch copies. ``--verify`` (which runs
the commit's tests locally, still no model and no network) proves each copy fails its oracle before the work and passes it on the
original commit; run it before trusting a case.

    python3 build_cases.py                                   # list the chosen commits
    python3 build_cases.py --build --out <scratch dir>       # make the copies and write cases.json beside them
    python3 build_cases.py --build --verify --out <scratch dir>

Standard library only. Nothing here names a person, a machine path or an identifier: the repository and the output folder are
arguments.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SMALL_MAX_LINES = 40         # source lines changed (added plus deleted), tests and docs not counted
MEDIUM_MAX_LINES = 200
PER_BAND = 6                 # 6 small plus 6 medium = 12
SOURCE_PREFIX = "quest_ai_runner/"
TEST_PREFIX = "tests/"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build"}


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def numstat(repo, sha):
    """(path, added, deleted) for each file of a commit; binary files are skipped."""
    rows = []
    for line in git(repo, "show", "--numstat", "--format=", sha).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((parts[2], int(parts[0]), int(parts[1])))
    return rows


def candidate(repo, sha):
    """The commit as a case, or None when it cannot be one: it must change library source AND add or change a test file, and
    touch nothing that is a recorded result."""
    if len(git(repo, "rev-list", "--parents", "-n", "1", sha).split()) != 2:
        return None                                  # a merge or a root commit
    files = numstat(repo, sha)
    source = [(p, a + d) for p, a, d in files if p.startswith(SOURCE_PREFIX) and p.endswith(".py")]
    tests = [p for p, _a, _d in files if p.startswith(TEST_PREFIX) and Path(p).name.startswith("test_") and p.endswith(".py")]
    if not source or not tests or any(p.endswith("results.csv") for p, _a, _d in files):
        return None
    lines = sum(n for _p, n in source)
    if lines > MEDIUM_MAX_LINES or lines < 3:
        return None
    return {"commit": sha, "source_files": [p for p, _n in source], "test_files": tests, "source_lines": lines,
            "band": "small" if lines <= SMALL_MAX_LINES else "medium"}


def choose(repo):
    """A deterministic pick: candidates ordered by a hash of the sha (no recency or author preference), PER_BAND of each band."""
    chosen = {"small": [], "medium": []}
    for sha in git(repo, "log", "--no-merges", "--format=%H", "-n", "3000").split():
        if all(len(v) >= PER_BAND for v in chosen.values()):
            break
        found = candidate(repo, sha)
        if found:
            chosen[found["band"]].append(found)
    pick = []
    for band in ("small", "medium"):
        pick += sorted(chosen[band], key=lambda c: hashlib.sha256(c["commit"].encode()).hexdigest())[:PER_BAND]
    return pick


def goal_text(case, subject):
    tests = " ".join(case["test_files"])
    return (f"Tests in {tests} fail in this working copy. Change the library source so they pass. Do not edit the tests. "
            f"The change they describe: {subject}")


def build_copy(repo, case, out):
    """A scratch copy at the commit with the SOURCE files put back to the parent's state and the commit's tests kept."""
    dest = out / case["id"]
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", str(repo), str(dest)], check=True)
    git(dest, "checkout", "--quiet", case["commit"])
    git(dest, "checkout", "--quiet", case["commit"] + "~1", "--", *case["source_files"])
    return dest


def oracle(case):
    return [sys.executable, "-m", "pytest", "-q", "-x", *case["test_files"]]


def passes(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[4]), help="this repository (default: the one holding this file)")
    ap.add_argument("--out", help="folder for the scratch copies and cases.json (required with --build)")
    ap.add_argument("--build", action="store_true", help="make the scratch copies (no model is run)")
    ap.add_argument("--verify", action="store_true", help="run each oracle locally: it must fail on the copy and pass on the commit")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    cases = choose(repo)
    for n, case in enumerate(cases, 1):
        case["id"] = f"LAUNCH-{n:02d}"
        case["subject"] = git(repo, "show", "-s", "--format=%s", case["commit"]).strip()
        case["goal"] = goal_text(case, case["subject"])
        case["oracle"] = oracle(case)
    if not args.build:
        for case in cases:
            print(case["id"], case["band"], case["source_lines"], case["commit"][:10], case["subject"][:80])
        print(f"{len(cases)} cases chosen (dry run: nothing was copied and nothing was run)")
        return 0
    if not args.out:
        ap.error("--build needs --out")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for case in cases:
        dest = build_copy(repo, case, out)
        case["workdir"] = str(dest)
        if args.verify:
            case["fails_before"] = not passes(case["oracle"], dest)
            git(dest, "checkout", "--quiet", case["commit"], "--", *case["source_files"])
            case["passes_after"] = passes(case["oracle"], dest)
            git(dest, "checkout", "--quiet", case["commit"] + "~1", "--", *case["source_files"])
    (out / "cases.json").write_text(json.dumps(cases, indent=1) + "\n", encoding="utf-8")
    if args.verify:
        bad = [c["id"] for c in cases if not (c["fails_before"] and c["passes_after"])]
        print("cases whose oracle does not discriminate (drop and replace them):", bad or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
