"""CURRENT: the live scanner, ``quest_ai_runner.structure.hygiene``. Text cases call ``scan_text``; repo cases call
``scan_repo`` over the real checkout (files git knows about, filtered by glob); command cases run
``python -m quest_ai_runner.structure.hygiene`` in a throwaway git repository. Zero model calls, zero network."""
import fnmatch
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from quest_ai_runner.structure import hygiene

REPO = Path(__file__).resolve().parents[4]


def matches(path, globs):
    return any(fnmatch.fnmatch(path, g) for g in globs)


def run_cli(args):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for name, parts in args["files"].items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("".join(parts))
        if args.get("gitignore"):
            (root / ".gitignore").write_text(args["gitignore"])
        if args.get("stage"):
            subprocess.run(["git", "add", "--", *args["stage"]], cwd=root, check=True)
        env = dict(os.environ, PYTHONPATH=str(REPO))
        done = subprocess.run([sys.executable, "-m", hygiene.__name__, "--root", str(root), *args.get("argv", [])],
                              capture_output=True, text=True, env=env, timeout=120)
    return {"exit": done.returncode, "text": done.stdout + done.stderr}


def build(meter):  # noqa: ARG001
    def scan(kind, args):
        if kind == "text":
            return {"patterns": sorted({f.pattern for f in hygiene.scan_text("".join(args["parts"]))})}
        if kind == "repo":
            files = [p for p in hygiene.repo_files(REPO)
                     if matches(str(p.relative_to(REPO)), args["include"])
                     and not matches(str(p.relative_to(REPO)), args.get("exclude", []))]
            return {"findings": [f"{f.path}:{f.line}: {f.pattern}" for f in hygiene.scan_repo(REPO, files)]}
        if kind == "cli":
            return run_cli(args)
        raise ValueError(kind)
    return scan
