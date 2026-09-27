"""The tool catalog from a shell: how a deep run calls the same tools the brain calls in-process.

    python -m quest_ai_runner.tools list [--query "email my team"] [--json]
    python -m quest_ai_runner.tools call send_quest_email --args '{"subject": "..", "body": ".."}'

The catalog is rebuilt from the same env the lane uses (``QUEST_*`` credentials for the standard
tools, ``QAR_TOOLS_FILE`` for custom ones), so a deep run sees exactly what the brain saw.
``--quest``/``--task`` (or ``QAR_TOOL_QUEST_ID``/``QAR_TOOL_TASK_ID``) fill a tool's context args
the way the in-process loop does. Exit codes: 0 success, 1 failure (reason printed), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from ..core.tools import ToolContext, build_tool_registry


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quest_ai_runner.tools", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="show the catalog, or the tools matching --query")
    p_list.add_argument("--query", help="show only tools relevant to this")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_call = sub.add_parser("call", help="invoke one tool")
    p_call.add_argument("name")
    p_call.add_argument("--args", default="{}", help="JSON object of arguments")
    p_call.add_argument("--args-file", help="read the JSON arguments from a file ('-' = stdin)")
    p_call.add_argument("--quest", default=os.getenv("QAR_TOOL_QUEST_ID"),
                        help="quest id for tools that take it from context")
    p_call.add_argument("--task", default=os.getenv("QAR_TOOL_TASK_ID"))
    args = parser.parse_args(argv)

    registry = build_tool_registry()

    if args.cmd == "list":
        specs = registry.search(args.query, k=len(registry) or 1) if args.query else registry.all()
        if args.json:
            print(json.dumps([{"name": s.name, "origin": s.origin, "mutates": s.mutates,
                               "description": s.description, "when_to_use": s.when_to_use,
                               "when_not_to_use": s.when_not_to_use,
                               "parameters": s.public_parameters(),
                               "auto_filled": s.auto_filled()} for s in specs], indent=2))
        else:
            print(registry.render_catalog(specs) if specs else "(no tools matched)")
        return 0

    try:
        if args.args_file:
            raw = sys.stdin.read() if args.args_file == "-" else open(args.args_file).read()
        else:
            raw = args.args
        call_args = json.loads(raw or "{}")
    except (OSError, ValueError) as e:
        print(f"Bad --args: {e}", file=sys.stderr)
        return 2
    result = registry.invoke(args.name, call_args,
                             ToolContext(quest_id=args.quest, task_id=args.task))
    print(result.text, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
