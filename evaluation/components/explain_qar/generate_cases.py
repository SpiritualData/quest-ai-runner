"""Regenerate ``cases.json`` from the repository: one case per Markdown document, per environment
variable the code reads, per CLI subcommand and per name in ``quest_ai_runner.__all__``.

    .venv/bin/python3 evaluation/components/explain_qar/generate_cases.py

Run it when a document, a variable, a subcommand or a public name is added; the ``coverage`` case fails until you do."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def documents():
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"], cwd=REPO,
                            capture_output=True, text=True).stdout.split()
    return sorted(f for f in set(listed) if not f.startswith("esoi/tracker/") and (REPO / f).is_file())


def main():
    import env_vars
    import quest_ai_runner
    from quest_ai_runner import cli_parser
    cases = []

    def add(kind, desc, args, expect):
        cases.append({"id": f"DOC-{len(cases) + 1:03d}", "area": kind, "kind": kind, "description": desc,
                      "args": args, "expect": expect})
    docs = documents()
    for f in docs:
        add("links", f"{f}: every relative link resolves", {"file": f}, {"eq": {"broken": []}})
    for f in docs:
        add("em_dash", f"{f}: no em dash", {"file": f}, {"eq": {"count": 0}})
    for f in [d for d in docs if d.startswith("docs/") and d.count("/") == 1 and d != "docs/README.md"]:
        add("indexed", f"{f}: linked from docs/README.md", {"file": f}, {"eq": {"indexed": True}})
    for var in env_vars.env_vars_in_code(REPO):
        add("env_documented", f"{var}: has a described row in docs/configuration.md", {"variable": var},
            {"eq": {"row": True, "described": True}})
    parser = cli_parser._build_parser()
    for action in parser._actions:
        for name in getattr(action, "choices", None) or {}:
            if action.__class__.__name__ == "_SubParsersAction":
                add("cli_documented", f"quest-ai-runner {name} is named in the docs", {"command": name},
                    {"eq": {"documented": True}})
    for name in quest_ai_runner.__all__:
        add("api_documented", f"{name} is named in the README or docs", {"name": name}, {"eq": {"documented": True}})
    add("coverage", "every document and variable has a case", {}, {"eq": {"uncovered": []}})
    (Path(__file__).parent / "cases.json").write_text(json.dumps(cases, indent=1))
    print(len(cases), "cases")


if __name__ == "__main__":
    main()
