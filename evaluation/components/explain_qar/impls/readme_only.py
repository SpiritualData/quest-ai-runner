"""BASELINE: documentation judged by the README alone. Only the README's own links are checked; every other
document, variable, subcommand and rule is unexamined (answered with an empty verdict). The floor a documentation
check must beat. Zero model calls."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def build(meter):  # noqa: ARG001
    def explain(kind, args):
        if kind == "links" and args["file"] == "README.md":
            text = (REPO / "README.md").read_text()
            bad = [t for t in LINK.findall(text) if not t.startswith(("http", "#", "mailto:"))
                   and not (REPO / t.split("#")[0]).exists()]
            return {"broken": sorted(set(bad))}
        return {}
    return explain
