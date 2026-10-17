"""Synthetic fixture repos for the check-the-structure evaluation (no real data, public-repo safe).

``baseline_repo(dest)`` writes a tiny repo that follows every structure rule: a package with core and
adapters layers, one port with one row in the registry, a recorded evaluation result, the generated docs.
The live checkers are COPIED in (``scripts/check_structure.py`` and its helpers, the leaderboard), so the
fixture is judged by the real code. ``apply_edits(root, edits)`` injects one known violation as a few
declarative edits; ``consumer_repo(dest)`` writes a minimal consumer registry for
``python -m quest_ai_runner.structure check-consumer``.
"""
import csv
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY = sys.executable
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO))
COPIED = ["scripts/check_structure.py", "scripts/env_vars.py", "scripts/build_tracker.py", "scripts/build_audit.py",
          "evaluation/components/leaderboard.py"]
COMPONENT_COLS = ["component_id", "layer", "path", "role", "interface", "metric", "docs", "tests", "rules", "gap",
                  "action", "status", "solutions", "metric_kind", "invariant_reason"]
SOLUTION_COLS = ["solution_id", "problem", "ideal", "port", "implementations", "selected", "selected_by", "metric",
                 "metric_command", "gap", "action"]
RESULT_COLS = ["date", "component", "impl", "cases", "passed", "dev", "heldout", "model_calls", "tokens_in",
               "tokens_out", "cost_usd", "seconds", "git_sha", "cases_sha", "model"]
METRIC = "evaluation/components/run.py greet --impl live"
PYTHON_SOURCES = {
    "quest_ai_runner/__init__.py": '"""Fixture package root."""\n',
    "quest_ai_runner/core/__init__.py": '"""Fixture core layer."""\n',
    "quest_ai_runner/core/adapters.py":
        '"""The fixture port."""\nfrom typing import Protocol\n\n\nclass Greeter(Protocol):\n'
        '    """Says hello."""\n\n    def greet(self, name: str) -> str: ...\n',
    "quest_ai_runner/core/selection_table.py":
        '"""Selection as data."""\nSELECTION = {"greet": {"default": "plain", "implementations": '
        '{"plain": "quest_ai_runner.adapters.plain:PlainGreeter"}}}\n',
    "quest_ai_runner/adapters/__init__.py": '"""Fixture adapters layer."""\n',
    "quest_ai_runner/adapters/plain.py":
        '"""The plain greeter."""\n\n\nclass PlainGreeter:\n    """Says hello plainly."""\n\n'
        '    def greet(self, name):\n        return f"hello {name}"\n',
    "tests/test_greet.py": '"""Pins the greeter."""\n',
    "evaluation/components/greet/score.py": '"""Scores a greeting."""\n',
    "evaluation/components/greet/impls/live.py":
        '"""CURRENT: the live greeter."""\nfrom quest_ai_runner.adapters.plain import PlainGreeter\n',
    "evaluation/components/greet/impls/shout.py": '"""FLOOR: shouts."""\n',
    "evaluation/components/greet/cases.json": "[]\n",
    "evaluation/components/run.py": '"""Stub runner."""\n',
    ".gitignore": "__pycache__/\n",
    "README.md": "# Fixture\n",
    "docs/README.md": "# Fixture docs\n",
    "docs/structure/README.md": "# Fixture structure\n",
}
COMPONENTS = [  # id, layer, path globs; every fixture component serves the one solution
    ("core", "core", "quest_ai_runner/core/*.py"),
    ("adapters", "adapters", "quest_ai_runner/adapters/*.py"),
    ("package-root", "config", "quest_ai_runner/__init__.py"),
    ("scripts", "scripts", "scripts/*.py"),
    ("structure-registry", "docs", "docs/structure/*;docs/structure/tracker/*"),
    ("docs", "docs", "docs/*.md;README.md;.gitignore"),
    ("tests", "tests", "tests/**"),
    ("eval", "evaluation", "evaluation/components/**"),
]


def csv_text(cols, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def component_row(cid, layer, path):
    return dict(component_id=cid, layer=layer, path=path, role="fixture role", interface="fixture interface",
                metric=METRIC, docs="README.md", tests="tests/test_greet.py", rules="2;9", gap="", action="",
                status="done", solutions="greet", metric_kind="eval", invariant_reason="")


def solution_row():
    return dict(solution_id="greet", problem="Greet a person", ideal="The greeting names the person",
                port="Greeter", implementations="quest_ai_runner/adapters/plain.py;evaluation/components/greet/impls/live.py;"
                "evaluation/components/greet/impls/shout.py", selected="quest_ai_runner/adapters/plain.py",
                selected_by="selection table", metric="names the person", metric_command=METRIC, gap="", action="")


def result_row(impl, passed, cases_sha="1234abcd"):
    return dict(date="2026-10-16", component="greet", impl=impl, cases="2", passed=str(passed), dev=f"{passed}/2",
                heldout="0/0", model_calls="0", tokens_in="0", tokens_out="0", cost_usd="0.0", seconds="0.0",
                git_sha="abc1234", cases_sha=cases_sha, model="none")


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def baseline_repo(dest):
    """Write the clean fixture repo, generate its docs with the live ``--write-docs``, return its path."""
    root = Path(dest)
    for rel in COPIED:
        write(root, rel, (REPO / rel).read_text())
    for rel, text in PYTHON_SOURCES.items():
        write(root, rel, text)
    write(root, "docs/structure/components.csv", csv_text(COMPONENT_COLS, [component_row(*c) for c in COMPONENTS]))
    write(root, "docs/structure/solutions.csv", csv_text(SOLUTION_COLS, [solution_row()]))
    write(root, "docs/structure/waivers.csv", csv_text(["rule", "path", "reason"], []))
    write(root, "docs/structure/principles.csv", csv_text(
        ["principle_id", "name", "statement", "how_measured"],
        [dict(principle_id="P2", name="Ontology", statement="Ideals and metrics", how_measured="(S7)")]))
    write(root, "docs/structure/env_descriptions.csv", csv_text(["variable", "description"], []))
    write(root, "evaluation/components/results.csv", csv_text(
        RESULT_COLS, [result_row("live", 2), result_row("shout", 1)]))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([PY, "scripts/check_structure.py", "--write-docs"], cwd=root, capture_output=True, check=False, env=ENV)
    return root


def read_rows(path):
    with Path(path).open(newline="") as fh:
        return list(csv.DictReader(fh))


def read_header(path):
    with Path(path).open(newline="") as fh:
        return next(csv.reader(fh))


def apply_edits(root, edits):
    """Apply declarative edits. ``write``/``append``/``replace``/``delete`` act on files; ``csv_cell``,
    ``csv_drop``, ``csv_dup`` and ``csv_append`` act on a CSV row found by ``key_col`` == ``key``;
    ``regen`` re-runs the live ``--write-docs`` so a case can isolate one rule from stale generated docs."""
    root = Path(root)
    for e in edits:
        op, rel = e["op"], e.get("path", "")
        path = root / rel
        if op == "write":
            write(root, rel, e["content"])
        elif op == "append":
            path.write_text(path.read_text() + e["content"])
        elif op == "replace":
            text = path.read_text()
            assert e["old"] in text, f"fixture edit does not apply: {e['old']!r} not in {rel}"
            path.write_text(text.replace(e["old"], e["new"], 1))
        elif op == "delete":
            path.unlink()
        elif op == "regen":
            subprocess.run([PY, "scripts/check_structure.py", "--write-docs"], cwd=root, capture_output=True, check=False, env=ENV)
        elif op.startswith("csv_"):
            rows = read_rows(path)
            cols = read_header(path)
            hit = next(r for r in rows if r[e["key_col"]] == e["key"]) if op != "csv_append" else None
            if op == "csv_cell":
                hit[e["col"]] = e["value"]
            elif op == "csv_drop":
                rows.remove(hit)
            elif op == "csv_dup":
                rows.append(dict(hit))
            elif op == "csv_append":
                rows.append({c: e["row"].get(c, "") for c in cols})
            path.write_text(csv_text(cols, rows))
        else:
            raise ValueError(op)


def copy_repo(src, dest):
    shutil.copytree(src, dest, symlinks=True)
    return Path(dest)


def consumer_repo(dest):
    """A consumer with one conforming port implementation, a registry, solutions and results CSVs."""
    root = Path(dest)
    write(root, "consumer_sink.py",
          '"""A fixture consumer sink."""\nfrom quest_ai_runner.core.adapters import EscalationSinkBase\n\n\n'
          'class Sink(EscalationSinkBase):\n    """Hands an escalation to nobody."""\n\n    def escalate(self, escalation):\n'
          '        return None\n')
    write(root, "tests/test_sink.py", '"""Pins the sink."""\n')
    write(root, "case_files/cases.json", "[]\n")
    from quest_ai_runner.structure.consumer_check import REQUIRED_COLUMNS, cases_hash
    cols = list(REQUIRED_COLUMNS) + ["solution_id"]
    row = dict(integration_id="sink", module="consumer_sink", symbol="Sink", qar_solution_id="escalate-to-a-human",
               qar_port="EscalationSink", kind="implementation", selected_by="hand-wired", metric="a live proof",
               tests="tests/test_sink.py", status="done", solution_id="escalate")
    write(root, "registry.csv", csv_text(cols, [row]))
    write(root, "solutions.csv", csv_text(["solution_id", "selected", "metric_files"],
                                          [dict(solution_id="escalate", selected="consumer_sink:Sink",
                                                metric_files="case_files/cases.json")]))
    write(root, "results.csv", csv_text(["solution_id", "cases", "passed", "git_sha", "cases_sha"],
                                        [dict(solution_id="escalate", cases="1", passed="1", git_sha="abc1234",
                                              cases_sha=cases_hash(["case_files/cases.json"], root))]))
    return root


def consumer_command(root, module="quest_ai_runner.structure"):
    return [PY, "-m", module, "check-consumer", "--registry", "registry.csv", "--root", ".",
            "--solutions", "solutions.csv", "--results", "results.csv"]


TEMPLATES = {}


def fresh_repo(kind, edits):
    """A private copy of the clean fixture (``repo`` or ``consumer``) with ``edits`` applied. The clean
    fixture is built once per process and copied for each case, so cases never see each other's damage."""
    import tempfile
    if kind not in TEMPLATES:
        template = Path(tempfile.mkdtemp(prefix=f"qar_structure_{kind}_"))
        (consumer_repo if kind == "consumer" else baseline_repo)(template)
        TEMPLATES[kind] = template
    root = copy_repo(TEMPLATES[kind], Path(tempfile.mkdtemp(prefix="qar_structure_case_")) / "r")
    apply_edits(root, edits)
    return root


def report(root, command):
    """Run a checker command in ``root`` and return its verdict: exit code and the problem lines."""
    done = subprocess.run(command, cwd=root, capture_output=True, text=True, env=ENV, timeout=120)
    problems = [ln for ln in done.stdout.splitlines() if ln and not ln.startswith(("structure check", "note:", "rows by kind"))]
    return {"exit": done.returncode, "problems": problems, "stderr": done.stderr[-300:]}


def audit_report(root):
    """Run the live ``scripts/build_audit.py`` in ``root`` and list every component whose verdict is not ``meets``."""
    done = subprocess.run([PY, "scripts/build_audit.py"], cwd=root, capture_output=True, text=True, env=ENV, timeout=120)
    if done.returncode:
        return {"exit": 2, "problems": [f"AUDIT crashed: {done.stderr[-200:]}"]}
    problems = [f"AUDIT {r['component_id']}: verdict {r['verdict']}, unmeasured [{r['unmeasured_solutions']}]"
                for r in read_rows(Path(root) / "docs/structure/audit.csv") if r["verdict"] != "meets"]
    return {"exit": 1 if problems else 0, "problems": problems}
