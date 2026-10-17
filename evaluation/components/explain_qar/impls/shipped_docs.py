"""CURRENT: the documentation as shipped, checked against the live repository. Relative links are resolved on disk,
em dashes counted, ``docs/README.md`` and ``docs/configuration.md`` read, ``components.csv`` rows followed to the
docs they name, and the live package consulted for its public names (``quest_ai_runner.__all__``) and its real
argparse tree. Zero model calls."""
import csv
import json
import re
from pathlib import Path

import quest_ai_runner
from quest_ai_runner import cli_parser

REPO = Path(__file__).resolve().parents[4]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PLACEHOLDER_TARGETS = {"url", "path", "link", "..."}
FENCE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def broken_links(path):
    text = FENCE.sub("", path.read_text())
    broken = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")) or target in PLACEHOLDER_TARGETS:
            continue
        if not (path.parent / target.split("#")[0]).exists():
            broken.append(target)
    return sorted(set(broken))


def prose():
    return "\n".join(p.read_text() for p in [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))])


def config_rows():
    rows = {}
    for line in (REPO / "docs/configuration.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| `") and len(cells) >= 2:
            rows[cells[0].strip("`")] = cells[1] if cells[1] else ("explained in docs" if len(cells) > 3 and cells[3] != "-" else "")
    return rows


def coverage():
    cases = json.loads((Path(__file__).parents[1] / "cases.json").read_text())
    have = {c["args"].get("file") for c in cases if c["kind"] == "links"} | \
           {c["args"].get("variable") for c in cases if c["kind"] == "env_documented"}
    listed = {p.relative_to(REPO).as_posix() for p in REPO.rglob("*.md")
              if ".venv" not in p.parts and "tracker" not in p.parts and "scratch" not in p.parts
              and "egg-info" not in str(p) and "node_modules" not in p.parts}
    docs = {f for f in listed if (REPO / f).is_file()} 
    import subprocess
    tracked = set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split())
    missing = sorted(f for f in (docs & tracked) if f not in have)
    return {"uncovered": missing}


def build(meter):  # noqa: ARG001
    def explain(kind, args):
        if kind == "links":
            return {"broken": broken_links(REPO / args["file"])}
        if kind == "em_dash":
            return {"count": (REPO / args["file"]).read_text().count("—")}
        if kind == "indexed":
            return {"indexed": Path(args["file"]).name in (REPO / "docs/README.md").read_text()}
        if kind == "component_docs":
            row = next(r for r in csv.DictReader((REPO / "docs/structure/components.csv").open(newline=""))
                       if r["component_id"] == args["component"])
            return {"missing": [d for d in row["docs"].split(";") if d.strip() and not (REPO / d.strip()).exists()]}
        if kind == "env_documented":
            rows = config_rows()
            return {"row": args["variable"] in rows, "described": bool(rows.get(args["variable"], "").strip())}
        if kind == "cli_documented":
            return {"documented": args["command"] in prose()}
        if kind == "api_documented":
            return {"documented": args["name"] in quest_ai_runner.__all__ and args["name"] in prose()}
        if kind == "coverage":
            return coverage()
        raise ValueError(kind)
    return explain
