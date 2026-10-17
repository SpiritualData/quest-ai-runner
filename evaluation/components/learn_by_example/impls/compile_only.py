"""BASELINE: a byte-compile smoke test. A file passes when it compiles. Nothing is imported, run, documented or
scanned, so an example that imports a removed name, crashes on start or leaks a secret still passes. The floor a
real example check must beat. Zero model calls."""
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[4] / "examples"


def build(meter):  # noqa: ARG001
    def run_example(kind, args):
        if kind == "compile":
            compile((EXAMPLES / args["file"]).read_text(), args["file"], "exec")
            return {"compiles": True}
        return {}
    return run_example
