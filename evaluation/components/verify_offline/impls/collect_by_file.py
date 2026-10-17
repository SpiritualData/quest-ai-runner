"""CURRENT: the live suite, collected for real. Every ``tests/`` file matching a shard is collected with
``pytest --collect-only``, one call per shard: test modules import, parametrisation resolves and no test body runs.
Seeded broken synthetic files prove the collector sees an import error, a syntax error, a bad parametrisation and
a module that raises on import. The size ceiling is read from ``docs/structure/waivers.csv`` (the S4 waivers).
Zero model calls."""
import csv
import tempfile
import uuid
from pathlib import Path

from quest_ai_runner import resources  # noqa: F401  (the suite is collected against the live package)
from verify_offline import collector

REPO = Path(__file__).resolve().parents[4]
MAX_LINES = 1500


def shard(args):
    files = sorted(REPO.glob(args["pattern"]))
    total, errors = collector.collect_files(files, REPO)
    return {"files": len(files), "collected": total, "errors": len(errors),
            "meets_floor": total >= args["min_collected"]}


def fixture(args):
    with tempfile.TemporaryDirectory() as tmp:
        unique = uuid.uuid4().hex[:8]   # a module name already imported by an earlier case would clash
        for name, source in args["files"].items():
            (Path(tmp) / f"test_{unique}_{name[5:]}").write_text(source)
        total, errors = collector.collect_files(sorted(Path(tmp).glob("test_*.py")), tmp)
    return {"collected": total, "errors": len(errors)}


def coverage(args):
    letters = tuple(args["shards"])
    return {"uncovered": sorted(p.name for p in (REPO / "tests").glob("test_*.py") if p.name[5] not in letters)}


def size(args):  # noqa: ARG001
    with (REPO / "docs/structure/waivers.csv").open(newline="") as fh:
        waived = {r["path"] for r in csv.DictReader(fh) if r["rule"] == "S4"}
    over = [str(p.relative_to(REPO)) for p in sorted((REPO / "tests").rglob("*.py"))
            if sum(1 for _ in p.open()) > MAX_LINES]
    return {"oversize": [p for p in over if p not in waived]}


def build(meter):  # noqa: ARG001
    def verify(kind, args):
        return {"shard": shard, "fixture": fixture, "size": size, "coverage": coverage}[kind](args)
    return verify
