"""``pip install quest-ai-runner[all]`` pulls every third-party library the hub's code imports (lazily or not).

The hub imports its model SDKs, httpx, mcp and the page libraries inside functions, so a missing declaration shows only when
that arm runs. This reads the hub's source, lists every non-standard-library module it imports, and checks that each one's
distribution is declared by QAR's core dependencies (``hubofhubs[locks,web]``) or by the ``all`` extra."""
import ast
import sys
import tomllib
from pathlib import Path

import hubofhubs

ROOT = Path(__file__).resolve().parents[1]
# Import name -> distribution name, for every library the hub is allowed to import. A new library must be added here AND declared.
DISTRIBUTION = {"anthropic": "anthropic", "google": "google-genai", "openai": "openai", "httpx": "httpx", "mcp": "mcp",
                "filelock": "filelock", "pypdf": "pypdf", "trafilatura": "trafilatura", "esoi": "esoi"}
# Pulled by the hub's own extras named in QAR's core dependency line (locks, web) and by esoi's line.
THROUGH_CORE = {"filelock", "httpx", "esoi"}


def imported_third_party(package_dir: Path) -> set:
    found = set()
    for path in package_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {name for name in found if name not in sys.stdlib_module_names and name != "hubofhubs"}


def declared_names(requirements) -> set:
    return {req.replace("[", " ").replace("=", " ").replace(">", " ").replace("<", " ").replace("@", " ").split()[0].lower() for req in requirements}


def test_every_library_the_hub_imports_is_declared_for_the_all_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    core = project["dependencies"]
    assert any(req.startswith("hubofhubs[locks,web]") for req in core), "the hub's locks and web extras are core"
    everything = declared_names(project["optional-dependencies"]["all"])
    imported = imported_third_party(Path(hubofhubs.__file__).parent)
    unknown = imported - set(DISTRIBUTION)
    assert not unknown, f"the hub imports {sorted(unknown)}: map them in DISTRIBUTION and declare them in the all extra"
    missing = {DISTRIBUTION[name] for name in imported if name not in THROUGH_CORE} - everything
    assert not missing, f"not in the all extra: {sorted(missing)}"
    assert "openai" in declared_names(project["optional-dependencies"]["openai"])
    assert "mcp==2.0.0" in project["optional-dependencies"]["mcp"]
