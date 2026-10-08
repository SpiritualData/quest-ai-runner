"""Guard against a function-local `import json` inside cli.main().

A local import anywhere in main() makes `json` a local name for the whole function, so an earlier
use in another subcommand (reconcile-plan's json.dumps) raises UnboundLocalError. Offline AST check.
"""
import ast
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "quest_ai_runner" / "cli.py"


def test_main_has_no_local_json_import():
    tree = ast.parse(CLI.read_text())
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    local_json = [
        node.lineno
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Import) and any(a.name == "json" and (a.asname or "json") == "json" for a in node.names)
    ]
    assert local_json == [], f"local `import json` inside main() at lines {local_json}"
