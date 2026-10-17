"""Synthetic inputs for the measurement cases: results tables and a mini component in a throwaway folder.
Nothing here touches the committed results.csv, solutions.csv or any real component."""
import contextlib
import csv
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

RESULT_COLUMNS = ["date", "component", "impl", "cases", "passed", "dev", "heldout", "model_calls",
                  "tokens_in", "tokens_out", "cost_usd", "seconds", "git_sha", "cases_sha", "model"]
SOLUTION_COLUMNS = ["solution_id", "problem", "ideal", "port", "implementations", "selected", "selected_by",
                    "metric", "metric_command", "gap", "action"]
MINI_CASES = [{"id": f"M{i}", "kind": "echo", "args": {"n": i}, "expect": {"eq": {"n": i}}, "heldout": i == 3}
              for i in (1, 2, 3)]
counter = itertools.count(1)


def full_row(partial, component="mini"):
    row = dict(date="2026-10-16", component=component, cases=10, passed=5, dev="", heldout="", model_calls=0,
               tokens_in=0, tokens_out=0, cost_usd=0.0, seconds=0.1, git_sha="abc1234", cases_sha="aaaa1111",
               model="none")
    row.update(partial)
    return row


def write_results(path, rows, component="mini"):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RESULT_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(full_row(r, component))


def write_solutions(path, selected, component="mini"):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SOLUTION_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerow({"solution_id": "mini-solution", "selected": selected,
                    "metric_command": f"evaluation/components/run.py {component} --impl {selected}"})


GOOD = '''def build(meter):
    def echo(kind, args):
        return {"n": args["n"]}
    return echo
'''
BAD = '''def build(meter):
    def echo(kind, args):
        return {"n": -1}
    return echo
'''
SPENDER = '''def build(meter):
    def echo(kind, args):
        meter.record()
        return {"n": args["n"]}
    return echo
'''
SCORE = '''import expect


def score_case(fn, case):
    return expect.score_kind_case(fn, case)
'''


@contextlib.contextmanager
def mini_component(cases_file="json"):
    """A mini component with three cases and impls ``good``, ``bad`` and ``spender`` (one model call per
    case), inside a temporary git repository whose ``quest_ai_runner`` folder stands for the code under test."""
    with tempfile.TemporaryDirectory(prefix="qar_measure_") as root:
        root = Path(root)
        name = f"minicomp{next(counter)}"
        comp = root / "evaluation" / "components" / name
        (comp / "impls").mkdir(parents=True)
        (root / "quest_ai_runner").mkdir()
        (root / "quest_ai_runner" / "live.py").write_text("X = 1\n")
        (comp / "__init__.py").write_text("")
        (comp / "impls" / "__init__.py").write_text("")
        (comp / "score.py").write_text(SCORE)
        for impl, text in (("good", GOOD), ("bad", BAD), ("spender", SPENDER)):
            (comp / "impls" / f"{impl}.py").write_text(text)
        if cases_file == "json":
            (comp / "cases.json").write_text(json.dumps(MINI_CASES))
        else:
            with open(comp / "cases.csv", "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["id", "kind", "heldout"], lineterminator="\n")
                w.writeheader()
                w.writerows({"id": c["id"], "kind": "echo", "heldout": "yes" if c["heldout"] else ""} for c in MINI_CASES)
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                                        cwd=root, capture_output=True, text=True, check=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        sys.path.insert(0, str(comp.parent))
        try:
            yield root, comp.parent, name
        finally:
            sys.path.remove(str(comp.parent))
            for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
                del sys.modules[mod]
