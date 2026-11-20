"""The tool catalog from a shell: how a deep run calls the same tools the brain calls in-process.

    python -m quest_ai_runner tools list [--query "email my team"] [--json]
    python -m quest_ai_runner tools call send_quest_email --kw subject="Weekly plan" --kw body="..."
    python -m quest_ai_runner tools call send_quest_email --args '{"subject": "..", "body": ".."}'
    python -m quest_ai_runner tools show update_outcome_progress

``--kw name=value`` (repeatable) is the shell-friendly form: a value that parses as JSON (a number, a
list, an object) is passed as that, anything else as the string. ``--args`` takes one JSON object for
nested values; ``--kw`` entries win over it. ``show NAME`` prints one tool's how-to.

The catalog is rebuilt from the same env the lane uses (``QUEST_*`` credentials for the standard
tools, ``QAR_TOOLS_FILE`` for custom ones), so a deep run sees exactly what the brain saw.
``--spec-root FOLDER`` (before the subcommand, or ``QAR_SPEC_ROOT``) is the repository whose specification records the
``find_specs`` family reads; those tools exist only where a root is found, and a worker's shell may start in another folder.
``--quest``/``--task``/``--run`` (or ``QAR_TOOL_QUEST_ID``/``QAR_TOOL_TASK_ID``/``QAR_TOOL_RUN_ID``) fill a
tool's context args the way the in-process loop does. Exit codes: 0 when the request was taken (applied, noop, or held for a person's approval; a held write is
waiting, not failed, so do not retry or file it again), 1 when it was refused, failed or its outcome is unknown (reason
printed), 2 usage. A line `status: <word>` follows the text for every status but applied.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import time
from typing import Optional

from quest_ai_runner.body.internal_context.quest.client.quest_client_transport import process_download_line
from quest_ai_runner.world.tools.tools import ToolContext, build_tool_registry
from quest_ai_runner.world.tools.tools_status import ACCEPTED_STATUSES, ToolStatus


def download_log_path() -> str:
    """``QAR_TOOL_DOWNLOAD_LOG``, else ``tool_downloads.log`` in the runner's state folder (beside ``lanes/``, so beside every lane.log)."""
    explicit = os.environ.get("QAR_TOOL_DOWNLOAD_LOG")
    if explicit:
        return explicit
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "quest-ai-runner", "tool_downloads.log")


def write_download_line(tool: str) -> None:
    """Append one line (tool, bytes downloaded, top endpoints) so shell tool calls are visible to the data-usage check. Never raises."""
    try:
        line = process_download_line(tool)
        if not line:
            return
        path = download_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line + "\n")
    except Exception:  # noqa: BLE001 -- accounting must never fail a tool call
        pass


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quest_ai_runner tools", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec-root", default=None, metavar="FOLDER",
                        help="the repository (a folder with an esoi.toml) whose specification records the spec tools read")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="show the catalog, or the tools matching --query")
    p_list.add_argument("--query", help="show only tools relevant to this")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_call = sub.add_parser("call", help="invoke one tool")
    p_call.add_argument("name")
    p_call.add_argument("--args", default="{}", help="JSON object of arguments")
    p_call.add_argument("--kw", action="append", default=[], metavar="NAME=VALUE",
                        help="one argument; repeatable. JSON values stay typed, the rest are strings")
    p_show = sub.add_parser("show", help="print one tool's description, when to use it and arguments")
    p_show.add_argument("name")
    p_call.add_argument("--args-file", help="read the JSON arguments from a file ('-' = stdin)")
    p_call.add_argument("--quest", default=os.getenv("QAR_TOOL_QUEST_ID"),
                        help="quest id for tools that take it from context")
    p_call.add_argument("--task", default=os.getenv("QAR_TOOL_TASK_ID"))
    p_call.add_argument("--run", default=os.getenv("QAR_TOOL_RUN_ID"),
                        help="this run's id, for tools that record against the run (record_metric_result)")
    args = parser.parse_args(argv)
    if args.spec_root:
        os.environ["QAR_SPEC_ROOT"] = args.spec_root   # before the catalog is built: the spec tools register from it

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

    if args.cmd == "show":
        spec = registry.get(args.name)
        if spec is None:
            close = difflib.get_close_matches(args.name, registry.names(), n=3, cutoff=0.5)
            print(f"No tool named {args.name!r}." + (f" Did you mean: {', '.join(close)}?" if close
                  else f" Available: {', '.join(registry.names()) or 'none'}"), file=sys.stderr)
            return 1
        print(registry.render_one(spec))
        return 0

    try:
        if args.args_file:
            raw = sys.stdin.read() if args.args_file == "-" else open(args.args_file).read()
        else:
            raw = args.args
        call_args = json.loads(raw or "{}")
        if not isinstance(call_args, dict):
            raise ValueError("expected a JSON object")
        for pair in args.kw:
            name, sep, value = pair.partition("=")
            if not sep or not name.strip():
                raise ValueError(f"--kw {pair!r} is not NAME=VALUE")
            try:
                call_args[name.strip()] = json.loads(value)
            except ValueError:
                call_args[name.strip()] = value
    except (OSError, ValueError) as e:
        print(f"Bad --args/--kw: {e}", file=sys.stderr)
        return 2
    # One shell call = one process: let the owning-team answer outlive it (beside the download log), so the next call asks nobody.
    os.environ.setdefault("QAR_QUEST_CACHE_DIR", os.path.join(os.path.dirname(download_log_path()), "cache"))
    try:
        result = registry.invoke(args.name, call_args,
                                 ToolContext(quest_id=args.quest, task_id=args.task,
                                             meta={"run_id": args.run} if args.run else {}))
    finally:
        write_download_line(args.name)
    accepted = result.status in ACCEPTED_STATUSES
    stream = sys.stdout if accepted else sys.stderr
    print(result.text, file=stream)
    if result.status is not ToolStatus.APPLIED:
        print(f"status: {result.status.value}", file=stream)
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
