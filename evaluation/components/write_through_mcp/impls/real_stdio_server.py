"""REAL SERVER (the single-case arm of ``write-through-mcp``): the production ``MCPWriteAdapter`` over the production ``MCPClient``,
talking MCP over stdio to a REAL server process (``write_through_mcp/real_server.py``, built on the MCP SDK) that this arm starts. The
process edge is no longer faked: tool discovery, the allowlist, namespacing, the ``tools/call`` request and the server's refusals or
errors all cross a real pipe, and the calls are counted by the server writing them to a file, not by a client stand-in. Zero model
calls, zero network (a child process on this host)."""
import json
import os
import sys
import tempfile
from pathlib import Path

from hubofhubs.act.mcp_client import MCPClient, MCPServerSpec
from quest_ai_runner.adapters.mcp_write_adapter import MCPWriteAdapter

SERVER = Path(__file__).resolve().parent.parent / "real_server.py"


def read_calls(log):
    if not os.path.exists(log):
        return []
    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines() if line.strip()]


def build(meter):  # noqa: ARG001
    def mcp_write(kind, args):
        with tempfile.TemporaryDirectory() as folder:
            log = os.path.join(folder, "calls.jsonl")
            spec_file = os.path.join(folder, "spec.json")
            Path(spec_file).write_text(json.dumps({"tools": args.get("tools", []), "results": args.get("results") or {},
                                                   "raises": args.get("raises") or [], "log": log}), encoding="utf-8")
            command = "/nonexistent/server" if args.get("discovery_fails") else sys.executable
            argv = [] if args.get("discovery_fails") else [str(SERVER), spec_file]
            spec = MCPServerSpec(alias="srv", transport="stdio", command=command, args=argv, timeout_s=20.0)
            client = MCPClient(spec, search_path=os.environ.get("PATH", ""))
            try:
                adapter = MCPWriteAdapter(alias=args.get("alias", "srv"), writable_tools=args.get("writable", []), spec=spec, client=client)
                if kind == "list":
                    return {"names": [op["name"] for op in adapter.list_writable_operations()]}
                if kind == "write":
                    name, targs = args["call"]
                    res = adapter.write_operation(name, targs)
                    calls = read_calls(log)
                    detail = res.detail or {}
                    return {"ok": res.ok, "n_calls": len(calls), "calls": calls, "detail_tool": detail.get("tool"), "error": bool(res.error)}
                raise ValueError(kind)
            finally:
                client.close()
    return mcp_write
