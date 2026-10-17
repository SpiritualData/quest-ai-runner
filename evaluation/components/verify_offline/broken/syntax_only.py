"""BASELINE: a syntax check. Every matching file is parsed with ``ast`` and counted as one 'test' per
``test_`` function found by name. Nothing is imported, so a file that raises ImportError, a missing fixture or a
broken parametrisation passes; parametrised tests are undercounted. The floor a collection check must beat.
Zero model calls."""
import ast
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def scan(paths):
    total, errors = 0, 0
    for path in paths:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            errors += 1
            continue
        total += sum(1 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
    return total, errors


def build(meter):  # noqa: ARG001
    def verify(kind, args):
        if kind == "shard":
            files = sorted(REPO.glob(args["pattern"]))
            total, errors = scan(files)
            return {"files": len(files), "collected": total, "errors": errors,
                    "meets_floor": total >= args["min_collected"]}
        if kind == "fixture":
            with tempfile.TemporaryDirectory() as tmp:
                for name, source in args["files"].items():
                    (Path(tmp) / name).write_text(source)
                total, errors = scan(sorted(Path(tmp).glob("test_*.py")))
            return {"collected": total, "errors": errors}
        if kind == "coverage":
            letters = tuple(args["shards"])
            return {"uncovered": sorted(p.name for p in (REPO / "tests").glob("test_*.py") if p.name[5] not in letters)}
        return {}
    return verify
